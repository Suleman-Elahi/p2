"""Raw ASGI Application Proxy for S3 Datapath Performance

Intercepts S3 GET/PUT single-object requests before Django middleware.
All header parsing is done once per request and shared across detection,
routing, auth, and the handler itself.
"""
import asyncio
import json
import logging
import os
import time
import urllib.parse

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.http import QueryDict

from p2.core.models import Volume
from p2.s3.auth.aws_v4 import AWSV4Authentication
from p2.s3.cache import get_cached_metadata, set_cached_metadata, invalidate_metadata
from p2.s3.engine import get_engine
from p2.s3.errors import AWSError
from p2.s3.fastpath import (
    cleanup_replaced_payload,
    existing_object_state,
    require_volume_permission,
    update_volume_stats_for_put,
    validate_fast_put_integrity,
)
from p2.s3.fileio import (
    fadvise_random,
    fadvise_sequential,
    mmap_read,
    open_noatime,
    read_file_optimized,
)
from p2.s3.meta_write import write_metadata
from p2.core.storage_path import blob_dir, blob_fs_path, blob_internal_path, ensure_dir, internal_to_fs
from p2.core.events import STREAM_BLOB_POST_SAVE, make_event, publish_event

try:
    from p2.s3 import p2_s3_crypto
except ImportError:
    p2_s3_crypto = None

LOGGER = logging.getLogger(__name__)

ATTR_BLOB_MIME = "blob.p2.io/mime"
ATTR_BLOB_SIZE_BYTES = "blob.p2.io/size/bytes"
ATTR_BLOB_IS_FOLDER = "blob.p2.io/is_folder"
ATTR_BLOB_STAT_MTIME = "blob.p2.io/stat/mtime"
ATTR_BLOB_STAT_CTIME = "blob.p2.io/stat/ctime"

# Cache the S3 base domain at module level — never changes at runtime.
_S3_BASE_DOMAIN: str | None = None

def _parse_stored_timestamp(ts_str: str):
    """Parse a stored mtime/ctime value, handling both ISO 8601 (current)
    and legacy Unix epoch floats from objects written before the timestamp fix."""
    from django.utils.dateparse import parse_datetime
    import datetime as _dt
    dt = parse_datetime(ts_str)
    if dt is None:
        try:
            dt = _dt.datetime.fromtimestamp(float(ts_str), tz=_dt.UTC)
        except (ValueError, OverflowError):
            return None
    return dt

def _get_s3_base_domain() -> str:
    global _S3_BASE_DOMAIN
    if _S3_BASE_DOMAIN is None:
        from p2.lib.config import CONFIG
        _S3_BASE_DOMAIN = CONFIG.y('s3.base_domain', 's3.example.com')
    return _S3_BASE_DOMAIN


# Pre-compute the empty SHA256 hash for GET/HEAD/OPTIONS auth verification.
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Reusable response templates — avoids dict construction per request.
_PUT_RESPONSE_START_TYPE = 'http.response.start'
_PUT_RESPONSE_BODY_TYPE = 'http.response.body'
_EMPTY_BODY = b''


def _apply_asgi_cors(resp_headers: list, volume, origin: str, method: str) -> None:
    if not origin or not volume:
        return
    try:
        from p2.s3.cors import get_cors_rules, find_matching_rule
        rules = get_cors_rules(volume)
        rule = find_matching_rule(rules, origin, method)
        if rule:
            resp_headers.append((b'access-control-allow-origin', origin.encode('utf-8')))
            resp_headers.append((b'access-control-allow-methods', ', '.join(rule.get('AllowedMethods', [])).encode('utf-8')))
            allowed_headers = rule.get('AllowedHeaders', [])
            if allowed_headers:
                resp_headers.append((b'access-control-allow-headers', ', '.join(allowed_headers).encode('utf-8')))
            expose_headers = rule.get('ExposeHeaders', [])
            if expose_headers:
                resp_headers.append((b'access-control-expose-headers', ', '.join(expose_headers).encode('utf-8')))
            max_age = rule.get('MaxAgeSeconds')
            if max_age:
                resp_headers.append((b'access-control-max-age', str(max_age).encode('utf-8')))
            resp_headers.append((b'vary', b'Origin'))
    except Exception as e:
        LOGGER.error("S3 ASGI CORS error: %s", e)


async def _s3_error(send, status, code, volume=None, origin=''):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code></Error>'.encode('utf-8')
    resp_headers = [
        (b'content-type', b'application/xml'),
        (b'content-length', str(len(xml)).encode('ascii'))
    ]
    if volume and origin:
        _apply_asgi_cors(resp_headers, volume, origin, 'GET')
    await send({
        'type': _PUT_RESPONSE_START_TYPE,
        'status': status,
        'headers': resp_headers
    })
    await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': xml})


def S3ProxyASGIApp(django_app):
    """ASGI wrapper: intercepts S3 GET/PUT, falls back to Django for everything else."""

    # Cache USE_X_ACCEL_REDIRECT and S3_ASYNC_EVENT_PUBLISH at startup.
    use_accel = getattr(settings, 'USE_X_ACCEL_REDIRECT', False)
    async_events = getattr(settings, 'S3_ASYNC_EVENT_PUBLISH', False)
    s3_base = _get_s3_base_domain()

    async def app(scope, receive, send):
        if scope['type'] != 'http':
            return await django_app(scope, receive, send)

        path = scope['path']
        method = scope['method']

        # Fast reject non-S3 paths without parsing headers.
        if path.startswith('/_/') or path.startswith('/api/') or path == '/favicon.ico' or path.startswith('/.well-known/'):
            return await django_app(scope, receive, send)

        # ── Parse headers ONCE ────────────────────────────────────────────
        # Build both the lowercase dict (for routing/PUT) and the META dict
        # (for auth) in a single pass over the raw ASGI headers.
        hdrs = {}          # lowercase str -> str
        meta = {           # Django-style META dict for auth
            'REQUEST_METHOD': method,
            'PATH_INFO': urllib.parse.unquote(path),
        }
        qs_bytes = scope.get('query_string', b'')
        qs = qs_bytes.decode('ascii')
        meta['QUERY_STRING'] = qs

        for raw_name, raw_value in scope.get('headers', []):
            k = raw_name.decode('ascii').lower()
            v = raw_value.decode('latin1')
            hdrs[k] = v
            if k == 'content-type':
                meta['CONTENT_TYPE'] = v
            elif k == 'content-length':
                meta['CONTENT_LENGTH'] = v
            meta[f'HTTP_{k.upper().replace("-", "_")}'] = v

        # ── S3 detection ──────────────────────────────────────────────────
        is_s3 = (
            'x-amz-date' in hdrs
            or hdrs.get('authorization', '').startswith('AWS')
            or 'X-Amz-Signature' in qs
            or 'X-P2-Signature' in qs
        )
        if not is_s3:
            host = hdrs.get('host', '').split(':')[0]
            if host.endswith('.' + s3_base):
                is_s3 = True

        if not is_s3:
            return await django_app(scope, receive, send)

        # ── Routing ───────────────────────────────────────────────────────
        if b'uploadId' in qs_bytes or b'tagging' in qs_bytes or b'acl' in qs_bytes:
            return await django_app(scope, receive, send)

        # Extract bucket + key
        host = hdrs.get('host', '').split(':')[0]
        if host.endswith('.' + s3_base):
            bucket = host[:-(len(s3_base) + 1)]
            key = urllib.parse.unquote(path.lstrip('/'))
        else:
            parts = path.lstrip('/').split('/', 1)
            if not parts or not parts[0]:
                return await django_app(scope, receive, send)
            bucket = parts[0]
            key = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ''

        if not bucket or not key:
            return await django_app(scope, receive, send)

        if method not in ('GET', 'PUT'):
            return await django_app(scope, receive, send)

        # ── Auth (reuses pre-built meta dict) ─────────────────────────────
        class _Req:
            __slots__ = ()
            nonlocal meta, method, path, qs
        _Req.method = method
        _Req.path = path
        _Req.META = meta
        _Req.GET = QueryDict(qs)
        _Req.body = _EMPTY_BODY

        try:
            if not AWSV4Authentication.can_handle(_Req):
                return await django_app(scope, receive, send)
            user = await AWSV4Authentication(_Req).validate()
            if not user:
                return await _s3_error(send, 403, 'AccessDenied')
        except AWSError as e:
            return await _s3_error(send, e.status, e.code)
        except Exception as e:
            LOGGER.error("S3 proxy auth error: %s", e)
            return await _s3_error(send, 500, 'InternalError')

        # ── Volume lookup ─────────────────────────────────────────────────
        try:
            from p2.s3.cache import get_cached_volume, set_cached_volume
            volume = get_cached_volume(bucket)
            if not volume:
                volume = await Volume.objects.aget(name=bucket)
                set_cached_volume(bucket, volume)
        except ObjectDoesNotExist:
            return await _s3_error(send, 404, 'NoSuchBucket')

        vol_hex = volume.uuid.hex
        origin = hdrs.get('origin', '')

        # ── GET ───────────────────────────────────────────────────────────
        if method == 'GET':
            try:
                await require_volume_permission(user, volume, 'read', bucket, key)
            except AWSError as e:
                return await _s3_error(send, e.status, e.code, volume=volume, origin=origin)

            attributes = get_cached_metadata(vol_hex, key)
            if attributes is None:
                engine = get_engine(volume)
                raw = await asyncio.to_thread(engine.get, key)
                if not raw:
                    return await _s3_error(send, 404, 'NoSuchKey', volume=volume, origin=origin)
                attributes = json.loads(raw)
                set_cached_metadata(vol_hex, key, attributes)

            ct = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
            size = attributes.get(ATTR_BLOB_SIZE_BYTES, '0')
            etag = attributes.get('blob.p2.io/hash/md5', '')
            ipath = attributes.get('internal_path', f"/internal-storage/volumes/{vol_hex}/{key}")

            # Check if object is compressed
            from p2.s3.compression import is_compressed, decompress
            object_compressed = is_compressed(attributes)
            # Use original size for content-length if compressed
            if object_compressed:
                original_size = attributes.get('blob.p2.io/original_size', size)
                size = original_size

            from email.utils import format_datetime
            lm = b""
            lm_str = attributes.get(ATTR_BLOB_STAT_MTIME, '')
            if lm_str:
                dt = _parse_stored_timestamp(lm_str)
                if dt:
                    lm = format_datetime(dt, usegmt=True).encode('ascii')

            # Skip X-Accel-Redirect for compressed objects (nginx can't decompress)
            if use_accel and 'x-real-ip' in hdrs and not object_compressed:
                resp_h = [
                    (b'x-accel-redirect', ipath.encode('utf-8')),
                    (b'x-p2-accel', b'1'),
                    (b'content-type', ct.encode('utf-8')),
                    (b'content-length', b'0'),
                    (b'accept-ranges', b'bytes'),
                ]
                if etag: resp_h.append((b'etag', f'"{etag}"'.encode('utf-8')))
                if lm: resp_h.append((b'last-modified', lm))
                
                _apply_asgi_cors(resp_h, volume, origin, method)
                
                await send({'type': _PUT_RESPONSE_START_TYPE, 'status': 200, 'headers': resp_h})
                await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': _EMPTY_BODY})
                return

            # Range request support (RFC 7233)
            # Range requests are not supported for compressed objects
            range_header = hdrs.get('range')
            if range_header and int(size) > 0 and not object_compressed:
                try:
                    unit, ranges = range_header.split('=', 1)
                    if unit.strip() != 'bytes':
                        raise ValueError
                    start_str, end_str = ranges.strip().split('-', 1)
                    start = int(start_str) if start_str else None
                    end = int(end_str) if end_str else None
                except (ValueError, AttributeError):
                    await _s3_error(send, 416, 'InvalidRange', volume=volume, origin=origin)
                    return

                total_size = int(size)
                if start is None:
                    start = max(0, total_size - end)
                    end = total_size - 1
                if end is None or end >= total_size:
                    end = total_size - 1
                if start > end or start >= total_size:
                    resp_h = [
                        (b'content-range', f'bytes */{total_size}'.encode('ascii')),
                    ]
                    await send({'type': _PUT_RESPONSE_START_TYPE, 'status': 416, 'headers': resp_h})
                    await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': _EMPTY_BODY})
                    return

                length = end - start + 1
                fs_path = internal_to_fs(ipath)
                resp_h = [
                    (b'content-type', ct.encode('utf-8')),
                    (b'content-length', str(length).encode('ascii')),
                    (b'content-range', f'bytes {start}-{end}/{total_size}'.encode('ascii')),
                    (b'accept-ranges', b'bytes'),
                ]
                if etag: resp_h.append((b'etag', f'"{etag}"'.encode('utf-8')))
                if lm: resp_h.append((b'last-modified', lm))
                _apply_asgi_cors(resp_h, volume, origin, method)

                try:
                    await send({'type': _PUT_RESPONSE_START_TYPE, 'status': 206, 'headers': resp_h})
                    # Optimized range read: fadvise(RANDOM) + pread for zero-seek overhead.
                    # Use memoryview to avoid copying bytearray into bytes.
                    def _read_range():
                        fd = open_noatime(fs_path)
                        try:
                            fadvise_random(fd)
                            buf = bytearray(length)
                            os.preadv(fd, [buf], start)
                            return buf
                        finally:
                            os.close(fd)
                    data = await asyncio.to_thread(_read_range)
                    await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': data, 'more_body': False})
                except OSError:
                    return await _s3_error(send, 404, 'NoSuchKey', volume=volume, origin=origin)
                return

            # Full body stream — optimized with fadvise + read_file_optimized
            fs_path = internal_to_fs(ipath)
            resp_h = [
                (b'content-type', ct.encode('utf-8')),
                (b'content-length', str(size).encode('ascii')),
                (b'accept-ranges', b'bytes'),
            ]
            if etag: resp_h.append((b'etag', f'"{etag}"'.encode('utf-8')))
            if lm: resp_h.append((b'last-modified', lm))
            
            _apply_asgi_cors(resp_h, volume, origin, method)
            
            total_size = int(size)
            SMALL_FILE_MAX = 256 * 1024  # mmap for files <= 256KB
            MEDIUM_FILE_MAX = 4 * 1024 * 1024  # pread for files <= 4MB
            STREAM_CHUNK = 4 * 1024 * 1024

            try:
                await send({'type': _PUT_RESPONSE_START_TYPE, 'status': 200, 'headers': resp_h})

                if total_size <= SMALL_FILE_MAX:
                    # Small file: mmap read (zero syscall, zero copy)
                    data = await asyncio.to_thread(mmap_read, fs_path)
                    if object_compressed:
                        data = decompress(data, True)
                    await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': data, 'more_body': False})
                elif total_size <= MEDIUM_FILE_MAX:
                    # Medium file: pread with fadvise(RANDOM) — no seek overhead
                    def _read_medium():
                        fd = open_noatime(fs_path)
                        try:
                            fadvise_random(fd)
                            buf = bytearray(total_size)
                            os.preadv(fd, [buf], 0)
                            return buf
                        finally:
                            os.close(fd)
                    data = await asyncio.to_thread(_read_medium)
                    if object_compressed:
                        data = decompress(data, True)
                    await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': data, 'more_body': False})
                else:
                    if object_compressed:
                        # Compressed large file: read all, decompress, send
                        def _read_compressed_large():
                            fd = open_noatime(fs_path)
                            try:
                                fadvise_sequential(fd)
                                data = os.read(fd, total_size)
                                return data
                            finally:
                                os.close(fd)
                        data = await asyncio.to_thread(_read_compressed_large)
                        data = decompress(data, True)
                        await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': data, 'more_body': False})
                    else:
                        # Large file: streaming with fadvise(SEQUENTIAL) + buffered read
                        def _stream_large():
                            fd = open_noatime(fs_path)
                            try:
                                fadvise_sequential(fd)
                                return fd
                            except Exception:
                                os.close(fd)
                                raise

                        fd = await asyncio.to_thread(_stream_large)
                        try:
                            remaining = total_size
                            while remaining > 0:
                                chunk_size = min(remaining, STREAM_CHUNK)
                                chunk = await asyncio.to_thread(os.read, fd, chunk_size)
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                                await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': chunk, 'more_body': remaining > 0})
                        finally:
                            os.close(fd)

            except OSError:
                return await _s3_error(send, 404, 'NoSuchKey', volume=volume, origin=origin)
            return

        # ── PUT ───────────────────────────────────────────────────────────
        if method == 'PUT':
            # If versioning is enabled, fall through to Django view which has
            # full version-aware archive/metadata logic. This keeps the fast
            # path simple for the common non-versioned case.
            if (volume.tags or {}).get('versioning') == 'true':
                return await django_app(scope, receive, send)

            try:
                await require_volume_permission(user, volume, 'write', bucket, key)
            except AWSError as e:
                return await _s3_error(send, e.status, e.code, volume=volume, origin=origin)

            client_ct = hdrs.get('content-type', 'application/octet-stream')
            try:
                content_length = int(hdrs.get('content-length', '-1'))
            except ValueError:
                return await _s3_error(send, 400, 'InvalidRequest', volume=volume, origin=origin)
            is_aws_chunked = (
                'aws-chunked' in hdrs.get('content-encoding', '')
                or 'x-amz-decoded-content-length' in hdrs
            )

            if content_length > 67108864 or content_length == -1:
                return await django_app(scope, receive, send)

            # Stream body directly to file while hashing — avoids buffering
            # the entire payload in memory. Peak memory = 1 chunk (4MB) instead
            # of full content_length (up to 64MB).
            blob_uuid = os.urandom(16).hex()
            dir_path = blob_dir(vol_hex, blob_uuid)
            ensure_dir(dir_path)
            fs_path = blob_fs_path(vol_hex, blob_uuid)
            ipath = blob_internal_path(vol_hex, blob_uuid)

            import hashlib
            CHUNK_SIZE = 4 * 1024 * 1024  # 4MB streaming chunks
            md5_hasher = hashlib.md5()
            sha256_hasher = hashlib.sha256()
            blob_size = 0

            try:
                fd = os.open(fs_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    while True:
                        message = await receive()
                        mtype = message['type']
                        if mtype == 'http.request':
                            chunk = message.get('body', _EMPTY_BODY)
                            if chunk:
                                if is_aws_chunked:
                                    from p2.s3.utils import decode_aws_chunked
                                    chunk = decode_aws_chunked(chunk)
                                os.write(fd, chunk)
                                md5_hasher.update(chunk)
                                sha256_hasher.update(chunk)
                                blob_size += len(chunk)
                            if not message.get('more_body', False):
                                break
                        elif mtype == 'http.disconnect':
                            os.close(fd)
                            try:
                                os.remove(fs_path)
                            except OSError:
                                pass
                            return
                finally:
                    os.close(fd)
            except OSError as e:
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
                return await _s3_error(send, 500, 'InternalError')

            final_md5 = md5_hasher.hexdigest()
            final_sha256 = sha256_hasher.hexdigest()

            # Validate integrity headers against computed hashes
            try:
                validate_fast_put_integrity(hdrs, b'', final_md5, final_sha256, blob_size=blob_size)
            except AWSError as e:
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
                return await _s3_error(send, e.status, e.code, volume=volume, origin=origin)

            # Metadata — use stdlib datetime for ISO 8601 without Django timezone overhead.
            import datetime as _dt
            now_iso = _dt.datetime.now(_dt.UTC).isoformat()

            # Check if object should be compressed
            from p2.s3.compression import should_compress, compress, get_compression_metadata
            compression_meta = {}
            if should_compress(blob_size):
                # Read the file back, compress, and overwrite
                def _compress_blob():
                    try:
                        with open(fs_path, 'rb') as f:
                            data = f.read()
                        compressed, was_compressed = compress(data)
                        if was_compressed:
                            with open(fs_path, 'wb') as f:
                                f.write(compressed)
                            return get_compression_metadata(blob_size, len(compressed))
                    except Exception:
                        pass
                    return {}
                compression_meta = await asyncio.to_thread(_compress_blob)
                if compression_meta:
                    # Update blob_size to compressed size for stats
                    blob_size = compression_meta.get('blob.p2.io/compressed_size', blob_size)

            metadata_json = json.dumps({
                ATTR_BLOB_MIME: client_ct,
                ATTR_BLOB_SIZE_BYTES: str(blob_size),
                ATTR_BLOB_IS_FOLDER: False,
                ATTR_BLOB_STAT_MTIME: now_iso,
                ATTR_BLOB_STAT_CTIME: now_iso,
                'blob.p2.io/hash/md5': final_md5,
                'blob.p2.io/hash/sha256': final_sha256,
                'internal_path': ipath,
                **compression_meta,
            })

            engine = get_engine(volume)
            existing_json, existing_size, existing_counted, old_internal_path = await existing_object_state(engine, key)
            try:
                await write_metadata(engine, key, metadata_json)
            except Exception:
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
                LOGGER.error("PUT metadata write failed for %s/%s, cleaned up blob", bucket, key)
                return await _s3_error(send, 500, 'InternalError')
            invalidate_metadata(vol_hex, key)
            if existing_json:
                from p2.s3.cache import invalidate_volume_global
                invalidate_volume_global(bucket)
            await cleanup_replaced_payload(old_internal_path, ipath)
            await update_volume_stats_for_put(volume, existing_counted, existing_size, blob_size)

            if async_events:
                event = make_event(blob_uuid=blob_uuid, volume_uuid=vol_hex, event_type='blob_post_save')
                event['blob_path'] = key
                event['mime'] = client_ct
                event['internal_path'] = ipath
                asyncio.create_task(publish_event(STREAM_BLOB_POST_SAVE, event))

            resp_h = [
                (b'etag', f'"{final_md5}"'.encode('utf-8')),
                (b'content-length', b'0'),
                (b'x-p2-put-fastpath', b'1'),
            ]
            _apply_asgi_cors(resp_h, volume, origin, method)
            await send({
                'type': _PUT_RESPONSE_START_TYPE,
                'status': 200,
                'headers': resp_h
            })
            await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': _EMPTY_BODY})
            return

    return app
