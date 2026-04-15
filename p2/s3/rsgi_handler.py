"""
RSGI S3 Datapath Handler

Intercepts S3 GET/PUT single-object requests at the RSGI layer before
Django is involved. Uses Granian's native proto API:

  GET  → proto.response_file()       zero-copy sendfile from Rust
  PUT  → async for chunk in proto    body streaming without ASGI overhead
  else → django_fallback(scope, proto)

Falls back to Django for: multipart, ACL, tagging, bucket ops, non-S3.
"""
import asyncio
import json
import logging
import time
import uuid
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
from p2.s3.meta_write import write_metadata
from p2.core.storage_path import (
    blob_dir, blob_fs_path, blob_internal_path, ensure_dir, internal_to_fs,
)
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers_dict(scope):
    """Return a lowercase str→str dict from RSGI scope headers."""
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
        bucket = host[: -(len(s3_base) + 1)]
        key = urllib.parse.unquote(scope.path.lstrip('/'))
        return bucket, key
    parts = scope.path.lstrip('/').split('/', 1)
    if not parts or not parts[0]:
        return None, None
    bucket = parts[0]
    key = urllib.parse.unquote(parts[1]) if len(parts) > 1 else ''
    return bucket, key


def _mock_request(scope, hdrs):
    """Minimal Django-like request for AWSV4Authentication."""
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


async def _error(proto, status, code):
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<Error><Code>{code}</Code></Error>'
    ).encode('utf-8')
    proto.response_bytes(
        status=status,
        headers=[('content-type', 'application/xml')],
        body=xml,
    )


# ── Main RSGI app factory ─────────────────────────────────────────────────────

def S3ProxyRSGIApp(django_fallback):
    """
    RSGI application that intercepts S3 GET/PUT single-object traffic.
    Everything else is forwarded to django_fallback(scope, proto).
    """

    async def app(scope, proto):
        if scope.proto != 'http':
            return await django_fallback(scope, proto)

        hdrs = _headers_dict(scope)

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
            return await _error(proto, 403, e.__class__.__name__)
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

        # ── GET ───────────────────────────────────────────────────────────────
        if method == 'GET':
            attributes = get_cached_metadata(volume.uuid.hex, key)
            if attributes is None:
                engine = get_engine(volume)
                metadata_json = await asyncio.to_thread(engine.get, key)
                if not metadata_json:
                    return await _error(proto, 404, 'NoSuchKey')
                attributes = json.loads(metadata_json)
                set_cached_metadata(volume.uuid.hex, key, attributes)

            content_type = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
            total_size = str(attributes.get(ATTR_BLOB_SIZE_BYTES, '0'))
            etag = attributes.get('blob.p2.io/hash/md5', '')
            internal_path = attributes.get(
                'internal_path',
                f"/internal-storage/volumes/{volume.uuid.hex}/{key}",
            )

            from email.utils import format_datetime
            from django.utils.dateparse import parse_datetime
            last_mod_str = attributes.get(ATTR_BLOB_STAT_MTIME, '')
            last_mod = ''
            if last_mod_str:
                dt = parse_datetime(last_mod_str)
                if dt:
                    last_mod = format_datetime(dt, usegmt=True)

            resp_headers = [
                ('content-type', content_type),
                ('content-length', total_size),
                ('accept-ranges', 'bytes'),
            ]
            if etag:
                resp_headers.append(('etag', f'"{etag}"'))
            if last_mod:
                resp_headers.append(('last-modified', last_mod))

            if getattr(settings, 'USE_X_ACCEL_REDIRECT', False):
                # Let nginx serve the file via X-Accel-Redirect.
                resp_headers.append(('x-accel-redirect', internal_path))
                resp_headers.append(('x-p2-accel', '1'))
                proto.response_empty(status=200, headers=resp_headers)
                return

            # Zero-copy file send via Granian's Rust sendfile.
            fs_path = internal_to_fs(internal_path)
            if not await asyncio.to_thread(lambda: __import__('os').path.exists(fs_path)):
                return await _error(proto, 404, 'NoSuchKey')
            proto.response_file(status=200, headers=resp_headers, file=fs_path)
            return

        # ── PUT ───────────────────────────────────────────────────────────────
        if method == 'PUT':
            content_length = int(hdrs.get('content-length', '-1'))
            content_encoding = hdrs.get('content-encoding', '')
            is_aws_chunked = (
                'aws-chunked' in content_encoding
                or 'x-amz-decoded-content-length' in hdrs
            )

            # Only fast-path contiguous uploads ≤ 64 MB
            if content_length > 64 * 1024 * 1024 or content_length == -1:
                return await django_fallback(scope, proto)

            # Read body via RSGI async iteration
            body_chunks = []
            async for chunk in proto:
                body_chunks.append(chunk)
            body = b''.join(body_chunks)

            if is_aws_chunked:
                from p2.s3.utils import decode_aws_chunked
                body = decode_aws_chunked(body)

            blob_uuid = uuid.uuid4().hex
            dir_path = blob_dir(volume.uuid.hex, blob_uuid)
            ensure_dir(dir_path)
            fs_path = blob_fs_path(volume.uuid.hex, blob_uuid)
            internal_path = blob_internal_path(volume.uuid.hex, blob_uuid)

            if p2_s3_crypto:
                final_md5, final_sha256 = await asyncio.to_thread(
                    p2_s3_crypto.write_and_hash_small, fs_path, body
                )
            else:
                import hashlib
                await asyncio.to_thread(lambda: open(fs_path, 'wb').write(body))
                final_md5 = hashlib.md5(body).hexdigest()
                final_sha256 = hashlib.sha256(body).hexdigest()

            from django.utils.timezone import now
            now_iso = str(now())
            client_ct = hdrs.get('content-type', 'application/octet-stream')
            metadata_payload = {
                ATTR_BLOB_MIME: client_ct,
                ATTR_BLOB_SIZE_BYTES: str(len(body)),
                ATTR_BLOB_IS_FOLDER: False,
                ATTR_BLOB_STAT_MTIME: now_iso,
                ATTR_BLOB_STAT_CTIME: now_iso,
                'blob.p2.io/hash/md5': final_md5,
                'blob.p2.io/hash/sha256': final_sha256,
                'internal_path': internal_path,
            }

            engine = get_engine(volume)
            await write_metadata(engine, key, json.dumps(metadata_payload))
            invalidate_metadata(volume.uuid.hex, key)

            if getattr(settings, 'S3_ASYNC_EVENT_PUBLISH', False):
                event = make_event(
                    blob_uuid=blob_uuid,
                    volume_uuid=volume.uuid.hex,
                    event_type='blob_post_save',
                )
                event.update({'blob_path': key, 'mime': client_ct, 'internal_path': internal_path})
                asyncio.create_task(publish_event(STREAM_BLOB_POST_SAVE, event))

            proto.response_empty(
                status=200,
                headers=[
                    ('etag', f'"{final_md5}"'),
                    ('content-length', '0'),
                    ('x-p2-put-fastpath', '1'),
                ],
            )
            return

    return app
