"""p2 S3 Object views"""
import logging
from email.utils import format_datetime
from xml.etree import ElementTree

from django.http.response import HttpResponse
from django.utils.dateparse import parse_datetime

from p2.core.acl import has_volume_permission
from p2.core.constants import (ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME,
                               ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_STAT_MTIME,
                               ATTR_BLOB_STAT_CTIME)
from p2.s3.constants import (TAG_S3_ACL, TAG_S3_USER_TAG_PREFIX,
                             XML_NAMESPACE)
from p2.s3.cors import apply_cors_headers, find_matching_rule, get_cors_rules
from p2.s3.errors import AWSAccessDenied, AWSBadDigest, AWSNoSuchKey
from p2.s3.http import XMLResponse
from p2.s3.presign import validate_presigned_token
from p2.s3.views.common import S3View
from p2.s3.views.multipart import MultipartUploadView
from p2.s3.utils import decode_aws_chunked, iter_request_body
from p2.s3.cache import get_cached_metadata, set_cached_metadata, invalidate_metadata
import json
import asyncio
from django.conf import settings
from django.http import StreamingHttpResponse

USE_ACCEL_REDIRECT = getattr(settings, 'USE_X_ACCEL_REDIRECT', False)


def _format_http_date(mtime_str: str) -> str | None:
    """Convert stored mtime string to RFC 7231 HTTP date format.
    Returns None when mtime_str is absent so callers can skip the header."""
    if not mtime_str:
        return None
    dt = parse_datetime(mtime_str)
    if dt is None:
        return None
    return format_datetime(dt, usegmt=True)



LOGGER = logging.getLogger(__name__)

# Canned ACL → p2 permission list mapping
_CANNED_ACL_PERMS = {
    "private":                  [],
    "public-read":              ["read"],
    "public-read-write":        ["read", "write"],
    "authenticated-read":       ["read"],
    "bucket-owner-read":        ["read"],
    "bucket-owner-full-control":["read", "write", "delete"],
}


def _log_event_publish_result(task: asyncio.Task) -> None:
    """Surface background publish failures without affecting request latency."""
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to publish blob event (background): %s", exc)



def _check_conditional_headers(request, blob) -> HttpResponse | None:
    """Check If-Match/If-None-Match/If-Modified-Since/If-Unmodified-Since.
    Returns an error HttpResponse if condition fails, None if OK."""
    if blob is None:
        if request.META.get('HTTP_IF_NONE_MATCH') == '*':
            return None
        if request.META.get('HTTP_IF_MATCH'):
            return HttpResponse(status=412)
        return None
    etag = blob.attributes.get('blob.p2.io/hash/md5', '')
    if_match = request.META.get('HTTP_IF_MATCH')
    if if_match:
        tags = [t.strip().strip('"') for t in if_match.split(',')]
        if etag.strip('"') not in tags and '*' not in tags:
            return HttpResponse(status=412)
    if_none_match = request.META.get('HTTP_IF_NONE_MATCH')
    if if_none_match:
        if if_none_match == '*':
            return HttpResponse(status=412)
        tags = [t.strip().strip('"') for t in if_none_match.split(',')]
        if etag.strip('"') in tags:
            return HttpResponse(status=412)
    if_unmod = request.META.get('HTTP_IF_UNMODIFIED_SINCE')
    if if_unmod:
        from email.utils import parsedate_to_datetime
        try:
            threshold = parsedate_to_datetime(if_unmod)
            mtime = blob.attributes.get(ATTR_BLOB_STAT_MTIME, '')
            if mtime:
                from django.utils.dateparse import parse_datetime
                blob_dt = parse_datetime(str(mtime))
                if blob_dt and blob_dt > threshold:
                    return HttpResponse(status=412)
        except (ValueError, TypeError):
            pass
    return None


def _user_tags_from_blob(blob: dict) -> dict:
    """Extract S3 user tags (s3.user/* prefix) from metadata dict tags."""
    tags = blob.get('tags', {}) if isinstance(blob, dict) else getattr(blob, 'tags', {})
    return {
        k[len(TAG_S3_USER_TAG_PREFIX):]: v
        for k, v in tags.items()
        if k.startswith(TAG_S3_USER_TAG_PREFIX)
    }


def _validate_checksum_headers(request, *, crc32_b64: str | None = None,
                               crc32c_b64: str | None = None,
                               sha256_hex: str | None = None,
                               sha1_b64: str | None = None):
    """Validate x-amz-checksum-* headers if present.

    Only validates algorithms that were computed by the caller.
    """
    expected = request.META.get('HTTP_X_AMZ_CHECKSUM_CRC32')
    if expected and crc32_b64 and expected != crc32_b64:
        raise AWSBadDigest
    expected = request.META.get('HTTP_X_AMZ_CHECKSUM_CRC32C')
    if expected and crc32c_b64 and expected != crc32c_b64:
        raise AWSBadDigest
    expected = request.META.get('HTTP_X_AMZ_CHECKSUM_SHA256')
    if expected and sha256_hex and expected != sha256_hex:
        raise AWSBadDigest
    expected = request.META.get('HTTP_X_AMZ_CHECKSUM_SHA1')
    if expected and sha1_b64 and expected != sha1_b64:
        raise AWSBadDigest


def _parse_tagging_xml(body: bytes) -> dict:
    """Parse a PutObjectTagging XML body into a flat dict."""
    root = ElementTree.fromstring(body)
    tags = {}
    for tag_el in root.iter("Tag"):
        key_el = tag_el.find("Key") or tag_el.find(f"{{{XML_NAMESPACE}}}Key")
        val_el = tag_el.find("Value") or tag_el.find(f"{{{XML_NAMESPACE}}}Value")
        if key_el is not None and key_el.text:
            tags[key_el.text] = val_el.text if val_el is not None else ""
    return tags


def _build_tagging_xml(tags: dict) -> ElementTree.Element:
    root = ElementTree.Element("{%s}Tagging" % XML_NAMESPACE)
    tag_set = ElementTree.SubElement(root, "TagSet")
    for k, v in tags.items():
        tag_el = ElementTree.SubElement(tag_set, "Tag")
        ElementTree.SubElement(tag_el, "Key").text = k
        ElementTree.SubElement(tag_el, "Value").text = str(v)
    return root


def _build_acl_xml(blob, owner_id: str, owner_name: str) -> ElementTree.Element:
    root = ElementTree.Element("{%s}AccessControlPolicy" % XML_NAMESPACE)
    owner = ElementTree.SubElement(root, "Owner")
    ElementTree.SubElement(owner, "ID").text = owner_id
    ElementTree.SubElement(owner, "DisplayName").text = owner_name
    acl_list = ElementTree.SubElement(root, "AccessControlList")
    canned = blob.tags.get(TAG_S3_ACL, "private")
    # Always add owner FULL_CONTROL
    grant = ElementTree.SubElement(acl_list, "Grant")
    grantee = ElementTree.SubElement(grant, "Grantee")
    grantee.set("{http://www.w3.org/2001/XMLSchema-instance}type", "CanonicalUser")
    ElementTree.SubElement(grantee, "ID").text = owner_id
    ElementTree.SubElement(grant, "Permission").text = "FULL_CONTROL"
    if "public-read" in canned or "public-read-write" in canned:
        grant2 = ElementTree.SubElement(acl_list, "Grant")
        grantee2 = ElementTree.SubElement(grant2, "Grantee")
        grantee2.set("{http://www.w3.org/2001/XMLSchema-instance}type", "Group")
        ElementTree.SubElement(grantee2, "URI").text = "http://acs.amazonaws.com/groups/global/AllUsers"
        ElementTree.SubElement(grant2, "Permission").text = "READ"
    return root


class ObjectView(S3View):
    """Object related views — all handlers are async."""

    async def _check_presigned(self, request, bucket: str, path: str):
        """If request carries a presigned token, validate it; skip normal AWS auth."""
        token = request.GET.get("X-P2-Signature")
        if not token:
            return
        max_age = int(request.GET.get("X-Amz-Expires", 3600))
        # Normalize: token key has no leading slash (matches URL router capture)
        validate_presigned_token(token, bucket, path.lstrip('/'), request.method, max_age=max_age)
        request._presigned_validated = True

    async def _apply_cors(self, request, response, volume):
        origin = request.META.get("HTTP_ORIGIN", "")
        if not origin:
            return response
        rules = get_cors_rules(volume)
        rule = find_matching_rule(rules, origin, request.method)
        if rule:
            apply_cors_headers(response, rule, origin)
        return response

    async def options(self, request, bucket, path):
        """CORS preflight."""
        origin = request.META.get("HTTP_ORIGIN", "")
        req_method = request.META.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD", "GET")
        try:
            volume = await self.get_volume(request.user, bucket, "read")
        except Exception:
            return HttpResponse(status=403)
        rules = get_cors_rules(volume)
        rule = find_matching_rule(rules, origin, req_method)
        if not rule:
            return HttpResponse(status=403)
        from p2.s3.cors import cors_preflight_response
        return cors_preflight_response(rule, origin)

    async def head(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectHEAD.html"""
        import asyncio
        await self._check_presigned(request, bucket, path)
        volume = await self.get_volume(request.user, bucket, 'read', object_key=path)

        from p2.s3.cache import get_cached_metadata, set_cached_metadata
        attributes = get_cached_metadata(volume.uuid.hex, path)
        if attributes is None:
            engine = await self.get_engine(volume)
            metadata_json = engine.get(path)
            if not metadata_json:
                return HttpResponse(status=404)
            import json
            attributes = json.loads(metadata_json)
            set_cached_metadata(volume.uuid.hex, path, attributes)

        await asyncio.sleep(0)

        response = HttpResponse(status=200)
        response['Content-Length'] = attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
        response['Content-Type'] = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        last_mod = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
        if last_mod:
            response['Last-Modified'] = last_mod
        etag = attributes.get('blob.p2.io/hash/md5', '')
        if etag:
            response['ETag'] = f'"{etag}"'
        response['Accept-Ranges'] = 'bytes'

        return await self._apply_cors(request, response, volume)

    async def get(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectGET.html"""
        await self._check_presigned(request, bucket, path)

        # Object tagging
        if 'tagging' in request.GET:
            return await self._get_tagging(request, bucket, path)
        # Object ACL
        if 'acl' in request.GET:
            return await self._get_acl(request, bucket, path)
        # List parts
        if 'uploadId' in request.GET:
            return await MultipartUploadView().dispatch(request, bucket, path)

        volume = await self.get_volume(request.user, bucket, 'read', object_key=path)

        attributes = get_cached_metadata(volume.uuid.hex, path)

        if attributes is None:
            engine = await self.get_engine(volume)
            metadata_json = engine.get(path)
            if not metadata_json:
                LOGGER.warning("GET 404: bucket=%s path=%r", bucket, path)
                return HttpResponse(status=404)
            attributes = json.loads(metadata_json)
            set_cached_metadata(volume.uuid.hex, path, attributes)
        
        content_type = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        total_size = int(attributes.get(ATTR_BLOB_SIZE_BYTES, 0))

        etag = attributes.get('blob.p2.io/hash/md5', '')
        # If-None-Match → 304 Not Modified
        if_none_match = request.META.get('HTTP_IF_NONE_MATCH')
        if if_none_match and etag:
            tags = [t.strip().strip('"') for t in if_none_match.split(',')]
            if etag.strip('"') in tags:
                resp = HttpResponse(status=304)
                resp['ETag'] = etag
                return await self._apply_cors(request, resp, volume)

        # X-Accel-Redirect: Django hands off to Nginx sendfile() — zero-copy.
        internal_path = attributes.get('internal_path', f"/internal-storage/volumes/{volume.uuid.hex}{path}")
        from p2.core.storage_path import internal_to_fs
        fs_path = internal_to_fs(internal_path)

        if USE_ACCEL_REDIRECT:
            # X-Accel-Redirect to Nginx — zero-copy sendfile path.
            # Do NOT set Content-Length here: the body is empty from uvicorn's
            # perspective. Nginx reads the file and sets the correct length itself.
            response = HttpResponse()
            response['X-Accel-Redirect'] = internal_path
            response['X-P2-Accel'] = '1'
            response['Content-Type'] = content_type
            last_mod = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
            if last_mod:
                response['Last-Modified'] = last_mod
            response['ETag'] = etag
            response['Accept-Ranges'] = 'bytes'
            if 'response-content-type' in request.GET:
                response['Content-Type'] = request.GET['response-content-type']
            if 'response-content-disposition' in request.GET:
                response['Content-Disposition'] = request.GET['response-content-disposition']
            return await self._apply_cors(request, response, volume)
        else:
            # Pure Python file serving — no Nginx dependency.
            # Tiered strategy to reduce threadpool overhead for small objects
            # and avoid loading large objects fully into memory.
            SMALL_SYNC_MAX = 64 * 1024
            MEDIUM_THREAD_MAX = 1024 * 1024
            STREAM_CHUNK_SIZE = 4 * 1024 * 1024

            import os
            if not os.path.exists(fs_path):
                return HttpResponse(status=404)

            if total_size <= SMALL_SYNC_MAX:
                try:
                    with open(fs_path, 'rb') as f:
                        data = f.read()
                    response = HttpResponse(data, content_type=content_type, status=200)
                except OSError:
                    return HttpResponse(status=404)
            elif total_size <= MEDIUM_THREAD_MAX:
                try:
                    data = await asyncio.to_thread(lambda: open(fs_path, 'rb').read())
                    response = HttpResponse(data, content_type=content_type, status=200)
                except OSError:
                    return HttpResponse(status=404)
            else:
                import aiofiles

                async def _file_stream():
                    async with aiofiles.open(fs_path, 'rb') as f:
                        while True:
                            chunk = await f.read(STREAM_CHUNK_SIZE)
                            if not chunk:
                                break
                            yield chunk

                response = StreamingHttpResponse(_file_stream(), content_type=content_type, status=200)

            response['Content-Length'] = total_size
            last_mod = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
            if last_mod:
                response['Last-Modified'] = last_mod
            response['ETag'] = etag
            response['Accept-Ranges'] = 'bytes'
            response['Accept-Ranges'] = 'bytes'
            if 'response-content-type' in request.GET:
                response['Content-Type'] = request.GET['response-content-type']
            if 'response-content-disposition' in request.GET:
                response['Content-Disposition'] = request.GET['response-content-disposition']
            elif content_type != 'application/octet-stream':
                response['Content-Disposition'] = 'inline'
            return await self._apply_cors(request, response, volume)


    async def post(self, request, bucket, path):
        return await MultipartUploadView().dispatch(request, bucket, path)

    async def put(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPUT.html"""
        await self._check_presigned(request, bucket, path)

        if 'uploadId' in request.GET:
            return await MultipartUploadView().dispatch(request, bucket, path)
        if 'tagging' in request.GET:
            return await self._put_tagging(request, bucket, path)
        if 'acl' in request.GET:
            return await self._put_acl(request, bucket, path)

        # Copy object — x-amz-copy-source header present
        copy_source = request.META.get('HTTP_X_AMZ_COPY_SOURCE')
        if copy_source:
            return await self._copy_object(request, bucket, path, copy_source)

        volume = await self.get_volume(request.user, bucket, 'write')
        import uuid
        import json
        from django.utils.timezone import now
        
        engine = await self.get_engine(volume)

        # Honour Content-Type from the client
        client_ct = request.META.get('CONTENT_TYPE', 'application/octet-stream')

        # Read body — decode aws-chunked if present
        # AWS SDKs use x-amz-decoded-content-length when sending chunked payloads
        import asyncio
        content_encoding = request.META.get('HTTP_CONTENT_ENCODING', '')
        decoded_length = request.META.get('HTTP_X_AMZ_DECODED_CONTENT_LENGTH')
        is_aws_chunked = 'aws-chunked' in content_encoding or decoded_length
        content_length_raw = request.META.get('CONTENT_LENGTH', '')
        try:
            content_length = int(content_length_raw) if content_length_raw else -1
        except (TypeError, ValueError):
            content_length = -1
        small_put_fast_path = (
            not is_aws_chunked and
            content_length >= 0 and
            content_length <= 64 * 1024
        )

        # Streaming high-throughput write
        blob_uuid = uuid.uuid4().hex
        from p2.core.storage_path import blob_dir, blob_fs_path, blob_internal_path, ensure_dir
        dir_path = blob_dir(volume.uuid.hex, blob_uuid)
        ensure_dir(dir_path)
        fs_path = blob_fs_path(volume.uuid.hex, blob_uuid)
        internal_path = blob_internal_path(volume.uuid.hex, blob_uuid)

        import binascii
        import base64
        import hashlib

        expected_crc32 = request.META.get('HTTP_X_AMZ_CHECKSUM_CRC32')
        expected_crc32c = request.META.get('HTTP_X_AMZ_CHECKSUM_CRC32C')
        expected_sha256 = request.META.get('HTTP_X_AMZ_CHECKSUM_SHA256')
        expected_sha1 = request.META.get('HTTP_X_AMZ_CHECKSUM_SHA1')

        md5_hasher = hashlib.md5()
        sha256_hasher = hashlib.sha256()
        sha1_hasher = hashlib.sha1() if expected_sha1 else None
        crc32_val = 0

        # CRC32C: only validate if Rust extension is available
        crc32c_buf = None
        _rust_cs = None
        if expected_crc32c:
            try:
                from p2.s3 import p2_s3_checksum as _rust_cs
                crc32c_buf = []
            except (ImportError, AttributeError):
                _rust_cs = None
                crc32c_buf = None

        blob_size = 0
        final_md5 = ""
        final_sha256 = ""
        md5_digest = b""

        if small_put_fast_path:
            # Hot path for tiny uploads: offload file IO and hashing entirely to Rust 
            # bypasses python context-switching and hashes concurrently.
            body = request.body
            blob_size = len(body)
            from p2.s3 import p2_s3_crypto
            # Rust extension releases the GIL — run synchronously for small objects
            # to avoid asyncio.to_thread dispatch overhead (~3ms under contention).
            final_md5, final_sha256 = p2_s3_crypto.write_and_hash_small(fs_path, body)
            md5_digest = binascii.unhexlify(final_md5)
            
            if sha1_hasher:
                sha1_hasher.update(body)
            if expected_crc32:
                crc32_val = binascii.crc32(body, crc32_val)
            if crc32c_buf is not None:
                crc32c_buf.append(body)
        else:
            import aiofiles
            async with aiofiles.open(fs_path, 'wb') as f:
                if is_aws_chunked:
                    body = await asyncio.to_thread(request.read)
                    body = decode_aws_chunked(body)
                    await f.write(body)
                    blob_size = len(body)
                    md5_hasher.update(body)
                    sha256_hasher.update(body)
                    if sha1_hasher:
                        sha1_hasher.update(body)
                    if expected_crc32:
                        crc32_val = binascii.crc32(body, crc32_val)
                    if crc32c_buf is not None:
                        crc32c_buf.append(body)
                else:
                    async for chunk in iter_request_body(request, 4 * 1024 * 1024):
                        await f.write(chunk)
                        blob_size += len(chunk)
                        md5_hasher.update(chunk)
                        sha256_hasher.update(chunk)
                        if sha1_hasher:
                            sha1_hasher.update(chunk)
                        if expected_crc32:
                            crc32_val = binascii.crc32(chunk, crc32_val)
                        if crc32c_buf is not None:
                            crc32c_buf.append(chunk)

            md5_digest = md5_hasher.digest()
            final_md5 = md5_hasher.hexdigest()
            final_sha256 = sha256_hasher.hexdigest()

        expected_md5 = request.META.get('HTTP_CONTENT_MD5')
        if expected_md5:
            computed_md5_b64 = base64.b64encode(md5_digest).decode('ascii')
            if computed_md5_b64 != expected_md5:
                raise AWSBadDigest

        crc32_b64 = None
        if expected_crc32:
            crc32_b64 = base64.b64encode(
                (crc32_val & 0xFFFFFFFF).to_bytes(4, byteorder='big', signed=False)
            ).decode('ascii')

        sha1_b64 = None
        if sha1_hasher:
            sha1_b64 = base64.b64encode(sha1_hasher.digest()).decode('ascii')

        crc32c_b64 = None
        if expected_crc32c and _rust_cs is not None and crc32c_buf is not None:
            crc32c_b64 = _rust_cs.compute_crc32c(b"".join(crc32c_buf))

        _validate_checksum_headers(
            request,
            crc32_b64=crc32_b64,
            crc32c_b64=crc32c_b64,
            sha256_hex=final_sha256 if expected_sha256 else None,
            sha1_b64=sha1_b64,
        )

        # Update and save attributes in LMDB (single put, no read-modify-write).
        existing_metadata_json = await asyncio.to_thread(engine.get, path)
        existing_size = 0
        existing_counted = False
        if existing_metadata_json:
            existing_attr = json.loads(existing_metadata_json)
            if not existing_attr.get(ATTR_BLOB_IS_FOLDER, False):
                existing_size = int(existing_attr.get(ATTR_BLOB_SIZE_BYTES, 0) or 0)
                existing_counted = True

        now_ts = str(now())
        metadata_payload = {
            ATTR_BLOB_MIME: client_ct,
            ATTR_BLOB_SIZE_BYTES: str(blob_size),
            ATTR_BLOB_IS_FOLDER: False,
            ATTR_BLOB_STAT_MTIME: now_ts,
            ATTR_BLOB_STAT_CTIME: now_ts,
            'blob.p2.io/hash/md5': final_md5,
            'blob.p2.io/hash/sha256': final_sha256,
            'internal_path': internal_path
        }

        metadata_json = json.dumps(metadata_payload)
        from p2.s3.meta_write import write_metadata
        await write_metadata(engine, path, metadata_json)
        
        # Invalidate metadata cache after write
        from p2.s3.cache import invalidate_metadata
        invalidate_metadata(volume.uuid.hex, path)
        from p2.core.volume_stats import adjust_volume_stats
        await adjust_volume_stats(
            volume,
            object_delta=0 if existing_counted else 1,
            bytes_delta=blob_size - existing_size,
        )

        # Publish event for background processing (webhooks, EXIF, etc.).
        # Optional non-blocking mode removes publish latency from the PUT critical path.
        try:
            from p2.core.events import STREAM_BLOB_POST_SAVE, make_event, publish_event
            event = make_event(
                blob_uuid=blob_uuid,
                volume_uuid=volume.uuid.hex,
                event_type="blob_post_save"
            )
            event['blob_path'] = path
            event['mime'] = client_ct
            event['internal_path'] = internal_path
            if getattr(settings, 'S3_ASYNC_EVENT_PUBLISH', False):
                task = asyncio.create_task(publish_event(STREAM_BLOB_POST_SAVE, event))
                task.add_done_callback(_log_event_publish_result)
            else:
                await publish_event(STREAM_BLOB_POST_SAVE, event)
        except Exception as e:
            LOGGER.warning("Failed to publish blob event: %s", e)

        response = HttpResponse(status=200)
        response['ETag'] = f'"{final_md5}"'
        response['X-P2-Put-FastPath'] = '1' if small_put_fast_path else '0'
        response['X-P2-Put-MetaQueue'] = '1' if getattr(settings, 'S3_METADATA_WRITE_QUEUE_ENABLED', False) else '0'
        return await self._apply_cors(request, response, volume)

    async def delete(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectDELETE.html"""
        await self._check_presigned(request, bucket, path)
        volume = await self.get_volume(request.user, bucket, 'delete')
        
        engine = await self.get_engine(volume)
        metadata_json = await asyncio.to_thread(engine.get, path)
        
        if metadata_json:
            import json, os
            attributes = json.loads(metadata_json)
            bytes_delta = 0
            object_delta = 0
            if not attributes.get(ATTR_BLOB_IS_FOLDER, False):
                bytes_delta = -int(attributes.get(ATTR_BLOB_SIZE_BYTES, 0) or 0)
                object_delta = -1
            internal_path = attributes.get('internal_path')
            if internal_path:
                from p2.core.storage_path import internal_to_fs
                fs_path = internal_to_fs(internal_path)
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
            await asyncio.to_thread(engine.delete, path)
            from p2.core.volume_stats import adjust_volume_stats
            await adjust_volume_stats(volume, object_delta=object_delta, bytes_delta=bytes_delta)
            
        return HttpResponse(status=204)

    # -------------------------------------------------------------------------
    # Range requests
    # -------------------------------------------------------------------------

    async def _range_response(self, request, blob, content_type, total_size, range_header, volume):
        """Handle Range: bytes=X-Y requests (RFC 7233)."""
        try:
            # Parse "bytes=start-end"
            unit, ranges = range_header.split('=', 1)
            if unit.strip() != 'bytes':
                raise ValueError
            start_str, end_str = ranges.strip().split('-', 1)
            start = int(start_str) if start_str else None
            end = int(end_str) if end_str else None
        except (ValueError, AttributeError):
            return HttpResponse(status=416)  # Range Not Satisfiable

        # Suffix range: bytes=-500 means last 500 bytes
        if start is None:
            start = max(0, total_size - end)
            end = total_size - 1
        if end is None or end >= total_size:
            end = total_size - 1
        if start > end or start >= total_size:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{total_size}'
            return response

        length = end - start + 1

        async def _ranged_stream():
            controller = blob.volume.storage.controller
            if isinstance(controller, AsyncStorageController):
                # Stream and skip/slice
                consumed = 0
                async for chunk in controller.get_read_stream(blob):
                    chunk_start = consumed
                    chunk_end = consumed + len(chunk)
                    if chunk_end <= start:
                        consumed = chunk_end
                        continue
                    if chunk_start >= end + 1:
                        break
                    # Slice the chunk to the requested range
                    slice_start = max(0, start - chunk_start)
                    slice_end = min(len(chunk), end + 1 - chunk_start)
                    yield memoryview(chunk)[slice_start:slice_end].tobytes()
                    consumed = chunk_end
            else:
                import asyncio
                data = await asyncio.to_thread(blob.read)
                yield data[start:end + 1]

        response = StreamingHttpResponse(_ranged_stream(), content_type=content_type, status=206)
        response['Content-Length'] = length
        response['Content-Range'] = f'bytes {start}-{end}/{total_size}'
        response['Accept-Ranges'] = 'bytes'
        return await self._apply_cors(request, response, volume)

    # -------------------------------------------------------------------------
    # Copy object
    # -------------------------------------------------------------------------

    async def _copy_object(self, request, dest_bucket: str, dest_path: str, copy_source: str):
        """PUT with x-amz-copy-source — copy blob within or across volumes."""
        import urllib.parse
        copy_source = urllib.parse.unquote(copy_source).lstrip('/')
        
        parts = copy_source.split('/', 1)
        if len(parts) != 2:
            return HttpResponse(status=400)
            
        src_bucket, src_path = parts
        
        src_volume = await self.get_volume(request.user, src_bucket, 'read')
        dest_volume = await self.get_volume(request.user, dest_bucket, 'write')
        
        src_engine = await self.get_engine(src_volume)
        dest_engine = await self.get_engine(dest_volume)
        
        src_json = await asyncio.to_thread(src_engine.get, src_path)
        if not src_json:
            return HttpResponse(status=404)
            
        import json, os, uuid, shutil
        import asyncio
        from django.utils.timezone import now
        
        src_attr = json.loads(src_json)
        src_internal_path = src_attr.get('internal_path')
        if not src_internal_path:
            return HttpResponse(status=404)
            
        src_fs = src_internal_path.replace('/internal-storage/', '/storage/')
        
        blob_uuid = uuid.uuid4().hex
        from p2.core.storage_path import storage_path, internal_to_fs
        src_fs = internal_to_fs(src_internal_path)
        dir_path = storage_path("volumes", dest_volume.uuid.hex, blob_uuid[0:2], blob_uuid[2:4])
        os.makedirs(dir_path, exist_ok=True)
        dest_fs = os.path.join(dir_path, blob_uuid)
        dest_internal_path = f"/internal-storage/volumes/{dest_volume.uuid.hex}/{blob_uuid[0:2]}/{blob_uuid[2:4]}/{blob_uuid}"
        
        try:
            await asyncio.to_thread(shutil.copy2, src_fs, dest_fs)
        except Exception as e:
            LOGGER.error("Failed to copy physical file: %s", e)
            return HttpResponse(status=500)
            
        dest_attr = src_attr.copy()
        dest_attr['internal_path'] = dest_internal_path
        dest_attr[ATTR_BLOB_STAT_MTIME] = str(now())
        dest_attr[ATTR_BLOB_STAT_CTIME] = str(now())
        
        existing_dest_json = await asyncio.to_thread(dest_engine.get, dest_path)
        existing_dest_size = 0
        existing_dest_counted = False
        if existing_dest_json:
            existing_dest_attr = json.loads(existing_dest_json)
            if not existing_dest_attr.get(ATTR_BLOB_IS_FOLDER, False):
                existing_dest_size = int(existing_dest_attr.get(ATTR_BLOB_SIZE_BYTES, 0) or 0)
                existing_dest_counted = True

        await asyncio.to_thread(dest_engine.put, dest_path, json.dumps(dest_attr))
        from p2.core.volume_stats import adjust_volume_stats
        await adjust_volume_stats(
            dest_volume,
            object_delta=0 if existing_dest_counted else 1,
            bytes_delta=int(dest_attr.get(ATTR_BLOB_SIZE_BYTES, 0) or 0) - existing_dest_size,
        )
        
        root = ElementTree.Element("{%s}CopyObjectResult" % XML_NAMESPACE)
        ElementTree.SubElement(root, "LastModified").text = dest_attr[ATTR_BLOB_STAT_MTIME]
        etag = dest_attr.get('blob.p2.io/hash/md5', '')
        if etag:
            ElementTree.SubElement(root, "ETag").text = f'"{etag}"'
            
        return XMLResponse(root)

    # -------------------------------------------------------------------------
    # Object tagging
    # -------------------------------------------------------------------------

    async def _get_tagging(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'read')
        engine = await self.get_engine(volume)
        meta = engine.get(path)
        if not meta: return HttpResponse(status=404)
        
        import json
        attr = json.loads(meta)
        tags = {k[len(TAG_S3_USER_TAG_PREFIX):]: v for k, v in attr.items() if k.startswith(TAG_S3_USER_TAG_PREFIX)}
        return XMLResponse(_build_tagging_xml(tags))

    async def _put_tagging(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'write')
        engine = await self.get_engine(volume)
        meta = engine.get(path)
        if not meta: return HttpResponse(status=404)
        
        body = request.body
        new_tags = _parse_tagging_xml(body)
        import json
        attr = json.loads(meta)
        
        for k in list(attr.keys()):
            if k.startswith(TAG_S3_USER_TAG_PREFIX):
                del attr[k]
                
        for k, v in new_tags.items():
            attr[f"{TAG_S3_USER_TAG_PREFIX}{k}"] = v
            
        engine.put(path, json.dumps(attr))
        return HttpResponse(status=200)

    async def _delete_tagging(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'write')
        engine = await self.get_engine(volume)
        meta = engine.get(path)
        if not meta: return HttpResponse(status=204)
        
        import json
        attr = json.loads(meta)
        changed = False
        for k in list(attr.keys()):
            if k.startswith(TAG_S3_USER_TAG_PREFIX):
                del attr[k]
                changed = True
                
        if changed:
            engine.put(path, json.dumps(attr))
        return HttpResponse(status=204)

    # -------------------------------------------------------------------------
    # Object ACL
    # -------------------------------------------------------------------------

    async def _get_acl(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'read')
        engine = await self.get_engine(volume)
        meta = engine.get(path)
        if not meta: return HttpResponse(status=404)
        
        import json
        attr = json.loads(meta)
        class StubBlob: pass
        b = StubBlob()
        b.tags = {TAG_S3_ACL: attr.get(TAG_S3_ACL, 'private')}
        
        owner_id = str(volume.owner.pk) if volume.owner else "0"
        owner_name = volume.owner.username if volume.owner else "System"
        return XMLResponse(_build_acl_xml(b, owner_id, owner_name))

    async def _put_acl(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'write')
        engine = await self.get_engine(volume)
        meta = engine.get(path)
        if not meta: return HttpResponse(status=404)
        
        acl_header = request.META.get('HTTP_X_AMZ_ACL')
        if not acl_header:
            return HttpResponse(status=200)
            
        import json
        attr = json.loads(meta)
        attr[TAG_S3_ACL] = acl_header
        engine.put(path, json.dumps(attr))
        return HttpResponse(status=200)
