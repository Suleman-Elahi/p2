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
import uuid
import urllib.parse

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.http import QueryDict

from p2.core.models import Volume
from p2.s3.auth.aws_v4 import AWSV4Authentication
from p2.s3.cache import get_cached_metadata, set_cached_metadata, invalidate_metadata
from p2.s3.engine import get_engine
from p2.s3.errors import AWSError
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


async def _s3_error(send, status, code):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code></Error>'.encode('utf-8')
    await send({
        'type': _PUT_RESPONSE_START_TYPE,
        'status': status,
        'headers': [(b'content-type', b'application/xml'), (b'content-length', str(len(xml)).encode('ascii'))]
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
        if path.startswith('/_/') or path == '/favicon.ico' or path.startswith('/.well-known/'):
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
            return await _s3_error(send, 403, e.__class__.__name__)
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

        # ── GET ───────────────────────────────────────────────────────────
        if method == 'GET':
            attributes = get_cached_metadata(vol_hex, key)
            if attributes is None:
                engine = get_engine(volume)
                raw = await asyncio.to_thread(engine.get, key)
                if not raw:
                    return await _s3_error(send, 404, 'NoSuchKey')
                attributes = json.loads(raw)
                set_cached_metadata(vol_hex, key, attributes)

            ct = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
            size = attributes.get(ATTR_BLOB_SIZE_BYTES, '0')
            etag = attributes.get('blob.p2.io/hash/md5', '')
            ipath = attributes.get('internal_path', f"/internal-storage/volumes/{vol_hex}/{key}")

            from email.utils import format_datetime
            from django.utils.dateparse import parse_datetime
            lm = b""
            lm_str = attributes.get(ATTR_BLOB_STAT_MTIME, '')
            if lm_str:
                dt = parse_datetime(lm_str)
                if dt:
                    lm = format_datetime(dt, usegmt=True).encode('ascii')

            if use_accel:
                resp_h = [
                    (b'x-accel-redirect', ipath.encode('utf-8')),
                    (b'x-p2-accel', b'1'),
                    (b'content-type', ct.encode('utf-8')),
                    (b'content-length', b'0'),
                    (b'accept-ranges', b'bytes'),
                ]
                if etag: resp_h.append((b'etag', f'"{etag}"'.encode('utf-8')))
                if lm: resp_h.append((b'last-modified', lm))
                await send({'type': _PUT_RESPONSE_START_TYPE, 'status': 200, 'headers': resp_h})
                await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': _EMPTY_BODY})
                return

            # Stream file directly
            import aiofiles
            fs_path = internal_to_fs(ipath)
            resp_h = [
                (b'content-type', ct.encode('utf-8')),
                (b'content-length', str(size).encode('ascii')),
                (b'accept-ranges', b'bytes'),
            ]
            if etag: resp_h.append((b'etag', f'"{etag}"'.encode('utf-8')))
            if lm: resp_h.append((b'last-modified', lm))
            try:
                await send({'type': _PUT_RESPONSE_START_TYPE, 'status': 200, 'headers': resp_h})
                async with aiofiles.open(fs_path, 'rb') as f:
                    while True:
                        chunk = await f.read(1048576)
                        if not chunk:
                            break
                        await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': chunk, 'more_body': True})
                await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': _EMPTY_BODY, 'more_body': False})
            except OSError:
                return await _s3_error(send, 404, 'NoSuchKey')
            return

        # ── PUT ───────────────────────────────────────────────────────────
        if method == 'PUT':
            client_ct = hdrs.get('content-type', 'application/octet-stream')
            content_length = int(hdrs.get('content-length', '-1'))
            is_aws_chunked = (
                'aws-chunked' in hdrs.get('content-encoding', '')
                or 'x-amz-decoded-content-length' in hdrs
            )

            if content_length > 67108864 or content_length == -1:
                return await django_app(scope, receive, send)

            # Read body — single receive() call for small objects
            body_chunks = []
            while True:
                message = await receive()
                mtype = message['type']
                if mtype == 'http.request':
                    body_chunks.append(message.get('body', _EMPTY_BODY))
                    if not message.get('more_body', False):
                        break
                elif mtype == 'http.disconnect':
                    return

            body = body_chunks[0] if len(body_chunks) == 1 else b''.join(body_chunks)
            if is_aws_chunked:
                from p2.s3.utils import decode_aws_chunked
                body = decode_aws_chunked(body)

            blob_uuid = uuid.uuid4().hex
            dir_path = blob_dir(vol_hex, blob_uuid)
            ensure_dir(dir_path)
            fs_path = blob_fs_path(vol_hex, blob_uuid)
            ipath = blob_internal_path(vol_hex, blob_uuid)

            # Write + hash
            blob_size = len(body)
            SYNC_THRESHOLD = 262144  # 256KB
            if p2_s3_crypto:
                if blob_size <= SYNC_THRESHOLD:
                    final_md5, final_sha256 = p2_s3_crypto.write_and_hash_small(fs_path, body)
                else:
                    final_md5, final_sha256 = await asyncio.to_thread(
                        p2_s3_crypto.write_and_hash_small, fs_path, body)
            else:
                import hashlib
                if blob_size <= SYNC_THRESHOLD:
                    with open(fs_path, 'wb') as _f:
                        _f.write(body)
                    final_md5 = hashlib.md5(body).hexdigest()
                    final_sha256 = hashlib.sha256(body).hexdigest()
                else:
                    await asyncio.to_thread(lambda: open(fs_path, 'wb').write(body))
                    final_md5 = hashlib.md5(body).hexdigest()
                    final_sha256 = hashlib.sha256(body).hexdigest()

            # Metadata
            from django.utils.timezone import now
            now_iso = str(now())
            metadata_json = json.dumps({
                ATTR_BLOB_MIME: client_ct,
                ATTR_BLOB_SIZE_BYTES: str(blob_size),
                ATTR_BLOB_IS_FOLDER: False,
                ATTR_BLOB_STAT_MTIME: now_iso,
                ATTR_BLOB_STAT_CTIME: now_iso,
                'blob.p2.io/hash/md5': final_md5,
                'blob.p2.io/hash/sha256': final_sha256,
                'internal_path': ipath,
            })

            engine = get_engine(volume)
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

            if async_events:
                event = make_event(blob_uuid=blob_uuid, volume_uuid=vol_hex, event_type='blob_post_save')
                event['blob_path'] = key
                event['mime'] = client_ct
                event['internal_path'] = ipath
                asyncio.create_task(publish_event(STREAM_BLOB_POST_SAVE, event))

            await send({
                'type': _PUT_RESPONSE_START_TYPE,
                'status': 200,
                'headers': [
                    (b'etag', f'"{final_md5}"'.encode('utf-8')),
                    (b'content-length', b'0'),
                    (b'x-p2-put-fastpath', b'1'),
                ]
            })
            await send({'type': _PUT_RESPONSE_BODY_TYPE, 'body': _EMPTY_BODY})
            return

    return app
