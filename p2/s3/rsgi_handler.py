"""
RSGI S3 Datapath Handler — Volume Pool Edition

Intercepts S3 GET/PUT single-object requests at the RSGI layer before
Django is involved. Uses Granian's native proto API:

  GET  → stream from volume pool via pread()
  PUT  → async body read → volume pool block allocation → group commit
  else → django_fallback(scope, proto)

Falls back to Django for: multipart, ACL, tagging, bucket ops, non-S3.
"""
import asyncio
import json
import logging
import os
import urllib.parse

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.http import QueryDict

from p2.core.models import Volume
from p2.s3.auth.aws_v4 import AWSV4Authentication
from p2.s3.cache import (
    get_cached_metadata, set_cached_metadata, invalidate_metadata,
    get_cached_volume, set_cached_volume,
)
from p2.s3.engine import get_engine
from p2.s3.errors import AWSError
from p2.s3.fastpath import (
    existing_object_state,
    require_volume_permission,
    update_volume_stats_for_put,
    validate_fast_put_integrity,
)
from p2.s3.volume_pool import BlockCoord, VolumePool
from p2.s3.volume_writer import write_block
from p2.s3.volume_reader import read_object, stream_blocks, total_size as _total_size
from p2.core.events import STREAM_BLOB_POST_SAVE, make_event, publish_event

try:
    from p2.s3 import p2_s3_crypto
except ImportError:
    p2_s3_crypto = None

LOGGER = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers_dict(scope):
    return {k.lower(): v for k, v in scope.headers.items()}


def _is_s3_request(scope, hdrs):
    path = scope.path
    if path.startswith('/.well-known/') or path == '/favicon.ico':
        return False
    if 'x-amz-date' in hdrs:
        return True
    auth = hdrs.get('authorization', '')
    if auth.startswith('AWS') or auth.startswith('Bearer'):
        return True
    qs = scope.query_string
    if 'X-Amz-Signature' in qs or 'X-P2-Signature' in qs:
        return True
    from p2.lib.config import CONFIG
    s3_base = CONFIG.y('s3.base_domain', 's3.example.com')
    host = hdrs.get('host', '').split(':')[0]
    if host.endswith('.' + s3_base):
        return True
    return False


def _extract_bucket_and_key(scope, hdrs):
    host = hdrs.get('host', '').split(':')[0]
    from p2.lib.config import CONFIG
    s3_base = CONFIG.y('s3.base_domain', 's3.example.com')
    if host.endswith('.' + s3_base):
        bucket = host[:-(len(s3_base) + 1)]
        key = urllib.parse.unquote(scope.path.lstrip('/'))
        return bucket, key
    parts = scope.path.lstrip('/').split('/', 1)
    if not parts or not parts[0]:
        return None, None
    bucket = parts[0]
    key = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ''
    return bucket, key


def _mock_request(scope, hdrs):
    class _R:
        method = scope.method
        path = scope.path
        META = {
            'REQUEST_METHOD': scope.method,
            'PATH_INFO': urllib.parse.unquote(scope.path),
            'QUERY_STRING': scope.query_string,
            **({'CONTENT_TYPE': hdrs['content-type']} if 'content-type' in hdrs else {}),
            **({'CONTENT_LENGTH': hdrs['content-length']} if 'content-length' in hdrs else {}),
            **{f"HTTP_{k.upper().replace('-','_')}": v for k, v in hdrs.items()},
        }
        GET = QueryDict(scope.query_string)
        body = b''
    return _R()


def _apply_rsgi_cors(resp_headers: list, volume, origin: str, method: str) -> None:
    if not origin or not volume:
        return
    try:
        from p2.s3.cors import get_cors_rules, find_matching_rule
        rules = get_cors_rules(volume)
        rule = find_matching_rule(rules, origin, method)
        if rule:
            resp_headers.append(('access-control-allow-origin', origin))
            resp_headers.append(('access-control-allow-methods', ', '.join(rule.get('AllowedMethods', []))))
            allowed_headers = rule.get('AllowedHeaders', [])
            if allowed_headers:
                resp_headers.append(('access-control-allow-headers', ', '.join(allowed_headers)))
            expose_headers = rule.get('ExposeHeaders', [])
            if expose_headers:
                resp_headers.append(('access-control-expose-headers', ', '.join(expose_headers)))
            max_age = rule.get('MaxAgeSeconds')
            if max_age:
                resp_headers.append(('access-control-max-age', str(max_age)))
            resp_headers.append(('vary', 'Origin'))
    except Exception as e:
        LOGGER.error("S3 RSGI CORS error: %s", e)


async def _error(proto, status, code, volume=None, origin=''):
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Error><Code>{code}</Code></Error>'
    ).encode('utf-8')
    headers = [('content-type', 'application/xml')]
    if volume and origin:
        _apply_rsgi_cors(headers, volume, origin, 'GET')
    proto.response_bytes(status=status, headers=headers, body=xml)


# ── Main RSGI app factory ─────────────────────────────────────────────────────

def S3ProxyRSGIApp(django_fallback):
    """RSGI application that intercepts S3 GET/PUT single-object traffic."""

    async def app(scope, proto):
        if scope.proto != 'http':
            return await django_fallback(scope, proto)

        hdrs = _headers_dict(scope)
        origin = hdrs.get('origin', '')

        if not _is_s3_request(scope, hdrs):
            return await django_fallback(scope, proto)

        method = scope.method
        qs = scope.query_string

        # Defer complex ops to Django
        if any(x in qs for x in ('uploadId', 'tagging', 'acl')):
            return await django_fallback(scope, proto)

        bucket, key = _extract_bucket_and_key(scope, hdrs)
        if not bucket or not key:
            return await django_fallback(scope, proto)

        if method not in ('GET', 'PUT'):
            return await django_fallback(scope, proto)

        # ── Auth ──────────────────────────────────────────────────────────────
        mock_req = _mock_request(scope, hdrs)
        try:
            if not AWSV4Authentication.can_handle(mock_req):
                return await django_fallback(scope, proto)
            user = await AWSV4Authentication(mock_req).validate()
            if not user:
                return await _error(proto, 403, 'AccessDenied')
        except AWSError as e:
            return await _error(proto, e.status, e.code)
        except Exception as e:
            LOGGER.error("S3 RSGI auth error: %s", e)
            return await _error(proto, 500, 'InternalError')

        # ── Volume lookup ─────────────────────────────────────────────────────
        try:
            volume = get_cached_volume(bucket)
            if not volume:
                volume = await Volume.objects.aget(name=bucket)
                set_cached_volume(bucket, volume)
        except ObjectDoesNotExist:
            return await _error(proto, 404, 'NoSuchBucket')

        pool = VolumePool.get()

        # ── GET ───────────────────────────────────────────────────────────────
        if method == 'GET':
            try:
                await require_volume_permission(user, volume, 'read', bucket, key)
            except AWSError as e:
                return await _error(proto, e.status, e.code, volume=volume, origin=origin)

            attributes = get_cached_metadata(volume.uuid.hex, key)
            if attributes is None:
                engine = get_engine(volume)
                metadata_json = await asyncio.to_thread(engine.get, key)
                if not metadata_json:
                    return await _error(proto, 404, 'NoSuchKey', volume=volume, origin=origin)
                attributes = json.loads(metadata_json)
                set_cached_metadata(volume.uuid.hex, key, attributes)

            # New schema fields
            content_type = attributes.get('mime', attributes.get('blob.p2.io/mime', 'application/octet-stream'))
            obj_size = int(attributes.get('size', attributes.get('blob.p2.io/size/bytes', 0)) or 0)
            etag = attributes.get('etag', attributes.get('blob.p2.io/hash/md5', ''))

            from email.utils import format_datetime
            from django.utils.dateparse import parse_datetime
            import datetime as _dt
            last_mod_str = attributes.get('mtime', attributes.get('blob.p2.io/stat/mtime', ''))
            last_mod = ''
            if last_mod_str:
                dt = parse_datetime(last_mod_str)
                if dt is None:
                    try:
                        dt = _dt.datetime.fromtimestamp(float(last_mod_str), tz=_dt.UTC)
                    except (ValueError, OverflowError):
                        pass
                if dt:
                    last_mod = format_datetime(dt, usegmt=True)

            resp_headers = [
                ('content-type', content_type),
                ('content-length', str(obj_size)),
                ('accept-ranges', 'bytes'),
            ]
            if etag:
                resp_headers.append(('etag', f'"{etag}"'))
            if last_mod:
                resp_headers.append(('last-modified', last_mod))
            _apply_rsgi_cors(resp_headers, volume, origin, method)

            blocks_raw = attributes.get('blocks', [])
            if blocks_raw:
                # New volume-pool schema: stream from block coords
                blocks = [BlockCoord.from_dict(b) for b in blocks_raw]
                if obj_size <= 4 * 1024 * 1024:
                    data = await read_object(pool, blocks)
                    proto.response_bytes(status=200, headers=resp_headers, body=data)
                else:
                    proto.response_str(status=200, headers=resp_headers)
                    async for chunk in stream_blocks(pool, blocks):
                        await proto.send_bytes(chunk)
                    await proto.close()
            else:
                # Legacy schema with internal_path — fall back to Django
                return await django_fallback(scope, proto)
            return

        # ── PUT ───────────────────────────────────────────────────────────────
        if method == 'PUT':
            if (volume.tags or {}).get('versioning') == 'true':
                return await django_fallback(scope, proto)

            try:
                content_length = int(hdrs.get('content-length', '-1'))
            except ValueError:
                return await _error(proto, 400, 'InvalidRequest')

            content_encoding = hdrs.get('content-encoding', '')
            is_aws_chunked = 'aws-chunked' in content_encoding or 'x-amz-decoded-content-length' in hdrs

            # Fast-path: only handle contiguous uploads ≤ 64 MiB
            if content_length > 64 * 1024 * 1024 or content_length == -1:
                return await django_fallback(scope, proto)

            try:
                await require_volume_permission(user, volume, 'write', bucket, key)
            except AWSError as e:
                return await _error(proto, e.status, e.code, volume=volume, origin=origin)

            body_chunks = []
            async for chunk in proto:
                body_chunks.append(chunk)
            body = b''.join(body_chunks)

            if is_aws_chunked:
                from p2.s3.utils import decode_aws_chunked
                body = decode_aws_chunked(body)

            import hashlib
            if p2_s3_crypto and len(body) <= 64 * 1024:
                final_md5, final_sha256 = await asyncio.to_thread(
                    p2_s3_crypto.write_and_hash_small, '/dev/null', body
                )
                # Don't write to file — we'll write to volume pool below
                final_md5 = hashlib.md5(body).hexdigest()
                final_sha256 = hashlib.sha256(body).hexdigest()
            else:
                final_md5 = hashlib.md5(body).hexdigest()
                final_sha256 = hashlib.sha256(body).hexdigest()

            try:
                validate_fast_put_integrity(hdrs, body, final_md5, final_sha256)
            except AWSError as e:
                return await _error(proto, e.status, e.code, volume=volume, origin=origin)

            engine = get_engine(volume)
            existing_json, existing_size, existing_counted = await existing_object_state(engine, key)

            # Allocate block in volume pool
            blob_size = len(body)
            import datetime as _dt
            now_iso = _dt.datetime.now(_dt.UTC).isoformat()
            client_ct = hdrs.get('content-type', 'application/octet-stream')

            if blob_size > 0:
                handle, offset = await asyncio.to_thread(pool.allocate_block, blob_size)
                block = BlockCoord(vol_uuid=handle.uuid_hex, offset=offset, length=blob_size)
                blocks = [block]
            else:
                handle = None
                offset = 0
                blocks = []

            metadata_payload = {
                'size': blob_size,
                'mime': client_ct,
                'blocks': [b.to_dict() for b in blocks],
                'etag': final_md5,
                'sha256': final_sha256,
                'mtime': now_iso,
                'ctime': now_iso,
                'is_folder': False,
            }
            meta_json = json.dumps(metadata_payload)

            if handle is not None:
                await write_block(handle, offset, body, engine, key, meta_json)
            else:
                await asyncio.to_thread(engine.put, key, meta_json)

            invalidate_metadata(volume.uuid.hex, key)
            if existing_json:
                from p2.s3.cache import invalidate_volume_global
                invalidate_volume_global(bucket)
            await update_volume_stats_for_put(volume, existing_counted, existing_size, blob_size)

            if getattr(settings, 'S3_ASYNC_EVENT_PUBLISH', False):
                event = make_event(
                    blob_uuid=os.urandom(8).hex(),
                    volume_uuid=volume.uuid.hex,
                    event_type='blob_post_save',
                )
                event.update({'blob_path': key, 'mime': client_ct, 'blocks': [b.to_dict() for b in blocks]})
                asyncio.create_task(publish_event(STREAM_BLOB_POST_SAVE, event))

            put_headers = [
                ('etag', f'"{final_md5}"'),
                ('content-length', '0'),
                ('x-p2-put-fastpath', '1'),
            ]
            _apply_rsgi_cors(put_headers, volume, origin, method)
            proto.response_empty(status=200, headers=put_headers)
            return

    return app
