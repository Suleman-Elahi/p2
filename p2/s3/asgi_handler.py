"""Raw ASGI Application Proxy for S3 Datapath Performance"""
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
from asgiref.sync import sync_to_async
try:
    from p2.s3 import p2_s3_crypto
except ImportError:
    p2_s3_crypto = None

LOGGER = logging.getLogger(__name__)

# Same as p2.s3.views.objects
ATTR_BLOB_MIME = "blob.p2.io/mime"
ATTR_BLOB_SIZE_BYTES = "blob.p2.io/size/bytes"
ATTR_BLOB_IS_FOLDER = "blob.p2.io/is_folder"
ATTR_BLOB_STAT_MTIME = "blob.p2.io/stat/mtime"
ATTR_BLOB_STAT_CTIME = "blob.p2.io/stat/ctime"

class MockDjangoRequest:
    """A lightweight mock request constructed directly from ASGI scope for AWSV4Authentication."""
    def __init__(self, scope):
        self.method = scope['method']
        self.path = scope['path']
        self.META = {
            'REQUEST_METHOD': self.method,
            'PATH_INFO': urllib.parse.unquote(self.path),
            'QUERY_STRING': scope.get('query_string', b'').decode('ascii'),
        }
        for name, value in scope.get('headers', []):
            try:
                k = name.decode('ascii').lower()
                v = value.decode('latin1')
                if k == 'content-type':
                    self.META['CONTENT_TYPE'] = v
                elif k == 'content-length':
                    self.META['CONTENT_LENGTH'] = v
                hdr = k.upper().replace('-', '_')
                self.META[f'HTTP_{hdr}'] = v
            except UnicodeDecodeError:
                pass

        # For auth checking
        self.GET = QueryDict(self.META['QUERY_STRING'])
        self.body = b"" # Only used for small DELETEs in auth hash check

async def s3_error_response(send, status_code, code_string):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code_string}</Code></Error>'.encode('utf8')
    await send({
        'type': 'http.response.start',
        'status': status_code,
        'headers': [(b'content-type', b'application/xml'), (b'content-length', str(len(xml)).encode('ascii'))]
    })
    await send({
        'type': 'http.response.body',
        'body': xml
    })

def _is_s3_request(scope):
    path = scope['path']
    if path.startswith('/.well-known/') or path == '/favicon.ico':
        return False
    
    headers = {k.decode('ascii').lower(): v.decode('latin1') for k, v in scope.get('headers', [])}
    if 'x-amz-date' in headers:
        return True
    
    auth = headers.get('authorization', '')
    if auth.startswith('AWS') or auth.startswith('Bearer'):
        return True
        
    query = scope.get('query_string', b'').decode('ascii')
    if 'X-Amz-Signature' in query or 'X-P2-Signature' in query:
        return True
        
    from p2.lib.config import CONFIG
    s3_base = CONFIG.y('s3.base_domain', 's3.example.com') # Fallback
    host = headers.get('host', '').split(':')[0]
    if host.endswith('.' + s3_base):
        return True
        
    return False

def _extract_bucket_and_path(scope):
    headers = {k.decode('ascii').lower(): v.decode('latin1') for k, v in scope.get('headers', [])}
    host = headers.get('host', '').split(':')[0]
    
    from p2.lib.config import CONFIG
    s3_base = CONFIG.y('s3.base_domain', 's3.example.com')
    if host.endswith('.' + s3_base):
        bucket = host.replace('.' + s3_base, '')
        key = urllib.parse.unquote(scope['path'].lstrip('/'))
        return bucket, key
        
    # Path-style fallback
    path_parts = scope['path'].lstrip('/').split('/', 1)
    if not path_parts or not path_parts[0]:
        return None, None
        
    bucket = path_parts[0]
    key = urllib.parse.unquote(path_parts[1]) if len(path_parts) > 1 else ''
    return bucket, key


def S3ProxyASGIApp(django_app):
    """
    Standard ASGI Application wrapper that intercepts and natively processes S3 GET/PUT traffic.
    Falls back to Django for control-plane endpoints, or complex S3 Multipart / ACL handling.
    """
    
    async def app(scope, receive, send):
        if scope['type'] != 'http':
            return await django_app(scope, receive, send)

        if not _is_s3_request(scope):
            return await django_app(scope, receive, send)

        # Ensure we only hijack raw data-plane path (GET single object, PUT single object)
        method = scope['method']
        query_string = scope.get('query_string', b'').decode('ascii')
        
        # Don't intercept multipart uploads, tagging or ACL operations yet 
        # (defer to django for complex control operations)
        if b'uploadId' in scope.get('query_string', b'') or b'tagging' in scope.get('query_string', b'') or b'acl' in scope.get('query_string', b''):
            return await django_app(scope, receive, send)

        bucket, key = _extract_bucket_and_path(scope)
        if not bucket or not key:
            # Maybe list bucket, fallback to Django
            return await django_app(scope, receive, send)

        if method not in ('GET', 'PUT'):
            return await django_app(scope, receive, send)

        # Authenticate
        mock_req = MockDjangoRequest(scope)
        try:
            if AWSV4Authentication.can_handle(mock_req):
                auth_handler = AWSV4Authentication(mock_req)
                user = await auth_handler.validate()
                if not user:
                    return await s3_error_response(send, 403, "AccessDenied")
            else:
                return await django_app(scope, receive, send)
        except AWSError as e:
            return await s3_error_response(send, 403, e.__class__.__name__)
        except Exception as e:
            LOGGER.error("S3 proxy auth error: %s", e)
            return await s3_error_response(send, 500, "InternalError")

        # Retrieve Volume
        try:
            from p2.s3.cache import get_cached_volume, set_cached_volume
            volume = get_cached_volume(bucket)
            if not volume:
                volume = await Volume.objects.aget(name=bucket)
                set_cached_volume(bucket, volume)
        except ObjectDoesNotExist:
            return await s3_error_response(send, 404, "NoSuchBucket")

        # GET Handler
        if method == 'GET':
            attributes = get_cached_metadata(volume.uuid.hex, key)
            engine = get_engine(volume)
            
            if attributes is None:
                metadata_json = await asyncio.to_thread(engine.get, key)
                if not metadata_json:
                    return await s3_error_response(send, 404, "NoSuchKey")
                attributes = json.loads(metadata_json)
                set_cached_metadata(volume.uuid.hex, key, attributes)
            
            content_type = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
            total_size = attributes.get(ATTR_BLOB_SIZE_BYTES, '0')
            etag = attributes.get('blob.p2.io/hash/md5', '')
            internal_path = attributes.get('internal_path', f"/internal-storage/volumes/{volume.uuid.hex}/{key}")
            
            # X-Accel-Redirect to Nginx
            from email.utils import format_datetime
            from django.utils.dateparse import parse_datetime
            
            last_mod_str = attributes.get('blob.p2.io/stat/mtime', '')
            last_mod_hdr = b""
            if last_mod_str:
                dt = parse_datetime(last_mod_str)
                if dt:
                    last_mod_hdr = format_datetime(dt, usegmt=True).encode('ascii')
            
            if getattr(settings, 'USE_X_ACCEL_REDIRECT', False):
                headers = [
                    (b'x-accel-redirect', internal_path.encode('utf-8')),
                    (b'x-p2-accel', b'1'),
                    (b'content-type', content_type.encode('utf-8')),
                    (b'content-length', b'0'),
                    (b'accept-ranges', b'bytes'),
                ]
                if etag:
                    headers.append((b'etag', f'"{etag}"'.encode('utf8')))
                if last_mod_hdr:
                    headers.append((b'last-modified', last_mod_hdr))
                
                await send({
                    'type': 'http.response.start',
                    'status': 200,
                    'headers': headers
                })
                await send({
                    'type': 'http.response.body',
                    'body': b''
                })
                return
            else:
                # If Nginx is not used, read directly (faster for developer testing)
                fs_path = internal_to_fs(internal_path)
                try:
                    data = await asyncio.to_thread(lambda: open(fs_path, 'rb').read())
                    headers = [
                        (b'content-type', content_type.encode('utf-8')),
                        (b'content-length', str(len(data)).encode('ascii')),
                        (b'accept-ranges', b'bytes'),
                    ]
                    if etag:
                        headers.append((b'etag', f'"{etag}"'.encode('utf8')))
                    if last_mod_hdr:
                        headers.append((b'last-modified', last_mod_hdr))
                        
                    await send({
                        'type': 'http.response.start',
                        'status': 200,
                        'headers': headers
                    })
                    await send({
                        'type': 'http.response.body',
                        'body': data
                    })
                except OSError:
                    return await s3_error_response(send, 404, "NoSuchKey")
                return

        # PUT Handler
        if method == 'PUT':
            headers = {k.decode('ascii').lower(): v.decode('latin1') for k, v in scope.get('headers', [])}
            client_ct = headers.get('content-type', 'application/octet-stream')
            content_length = int(headers.get('content-length', '-1'))
            content_encoding = headers.get('content-encoding', '')
            decoded_length_raw = headers.get('x-amz-decoded-content-length', '')
            is_aws_chunked = 'aws-chunked' in content_encoding or decoded_length_raw
            
            # For this hyper-optimized datapath, we only support contiguous uploads < 64MB right now
            if content_length > 64 * 1024 * 1024 or content_length == -1:
                return await django_app(scope, receive, send)

            # Slurp Body directly from ASGI
            body_chunks = []
            while True:
                message = await receive()
                if message['type'] == 'http.request':
                    body_chunks.append(message.get('body', b''))
                    if not message.get('more_body', False):
                        break
                elif message['type'] == 'http.disconnect':
                    return

            body = b"".join(body_chunks)
            if is_aws_chunked:
                from p2.s3.utils import decode_aws_chunked
                body = decode_aws_chunked(body)
            
            blob_size = len(body)
            blob_uuid = uuid.uuid4().hex
            
            dir_path = blob_dir(volume.uuid.hex, blob_uuid)
            ensure_dir(dir_path)
            fs_path = blob_fs_path(volume.uuid.hex, blob_uuid)
            internal_path = blob_internal_path(volume.uuid.hex, blob_uuid)
            
            # Fastpath offload to Rust
            if p2_s3_crypto:
                final_md5, final_sha256 = await asyncio.to_thread(p2_s3_crypto.write_and_hash_small, fs_path, body)
            else:
                import hashlib
                await asyncio.to_thread(lambda: open(fs_path, 'wb').write(body))
                final_md5 = hashlib.md5(body).hexdigest()
                final_sha256 = hashlib.sha256(body).hexdigest()

            # Record Meta
            now_ts = str(time.time()) # Not ISO but sufficient for stat
            from django.utils.timezone import now
            now_ts_iso = str(now())
            metadata_payload = {
                ATTR_BLOB_MIME: client_ct,
                ATTR_BLOB_SIZE_BYTES: str(blob_size),
                ATTR_BLOB_IS_FOLDER: False,
                ATTR_BLOB_STAT_MTIME: now_ts_iso,
                ATTR_BLOB_STAT_CTIME: now_ts_iso,
                'blob.p2.io/hash/md5': final_md5,
                'blob.p2.io/hash/sha256': final_sha256,
                'internal_path': internal_path
            }
            
            engine = get_engine(volume)
            metadata_json = json.dumps(metadata_payload)
            await write_metadata(engine, key, metadata_json)
            invalidate_metadata(volume.uuid.hex, key)

            if getattr(settings, 'S3_ASYNC_EVENT_PUBLISH', False):
                event = make_event(
                    blob_uuid=blob_uuid,
                    volume_uuid=volume.uuid.hex,
                    event_type="blob_post_save"
                )
                event['blob_path'] = key
                event['mime'] = client_ct
                event['internal_path'] = internal_path
                asyncio.create_task(publish_event(STREAM_BLOB_POST_SAVE, event))

            await send({
                'type': 'http.response.start',
                'status': 200,
                'headers': [
                    (b'etag', f'"{final_md5}"'.encode('utf8')),
                    (b'content-length', b'0'),
                    (b'x-p2-put-fastpath', b'1')
                ]
            })
            await send({
                'type': 'http.response.body',
                'body': b''
            })
            return

    return app
