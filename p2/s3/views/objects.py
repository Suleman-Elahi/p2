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
from p2.s3.fastpath import cleanup_replaced_payload
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
    Returns None when mtime_str is absent so callers can skip the header.
    Handles both ISO 8601 (current) and legacy Unix epoch floats."""
    if not mtime_str:
        return None
    dt = parse_datetime(mtime_str)
    if dt is None:
        # Legacy: Unix epoch floats from objects written before timestamp fix.
        import datetime as _dt
        try:
            dt = _dt.datetime.fromtimestamp(float(mtime_str), tz=_dt.UTC)
        except (ValueError, OverflowError):
            return None
    return format_datetime(dt, usegmt=True) if dt else None



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
    if expected and sha256_hex:
        import base64
        import binascii
        sha256_b64 = base64.b64encode(binascii.unhexlify(sha256_hex)).decode('ascii')
        if expected != sha256_b64:
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
            from p2.s3.cache import get_cached_volume, set_cached_volume
            from p2.core.models import Volume
            volume = get_cached_volume(bucket)
            if not volume:
                volume = await Volume.objects.aget(name=bucket)
                set_cached_volume(bucket, volume)
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

        requested_version_id = request.GET.get('versionId')
        if requested_version_id:
            engine = await self.get_engine(volume)
            from p2.s3.versioning import _version_lmdb_key
            lmdb_key = _version_lmdb_key(path, requested_version_id)

            def _get_version_meta():
                with engine.env.begin(db=engine.db) as txn:
                    val = txn.get(lmdb_key)
                    return val

            raw_val = await asyncio.to_thread(_get_version_meta)
            if raw_val is None:
                return HttpResponse(status=404)
            attributes = json.loads(raw_val)
            if attributes.get('blob.p2.io/delete_marker', False):
                response = HttpResponse(status=404)
                response['x-amz-delete-marker'] = 'true'
                response['x-amz-version-id'] = requested_version_id
                return await self._apply_cors(request, response, volume)
        else:
            from p2.s3.cache import get_cached_metadata, set_cached_metadata
            attributes = get_cached_metadata(volume.uuid.hex, path)
            if attributes is None:
                engine = await self.get_engine(volume)
                metadata_json = engine.get(path)
                if not metadata_json:
                    from p2.s3.versioning import list_versions
                    versions = await list_versions(engine, prefix=path, max_keys=1)
                    if versions and versions[0]['key'] == path and versions[0]['is_delete_marker']:
                        response = HttpResponse(status=404)
                        response['x-amz-delete-marker'] = 'true'
                        response['x-amz-version-id'] = versions[0]['version_id']
                        return await self._apply_cors(request, response, volume)
                    return HttpResponse(status=404)
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
        if requested_version_id:
            response['x-amz-version-id'] = requested_version_id
        elif attributes.get('blob.p2.io/version_id'):
            response['x-amz-version-id'] = attributes.get('blob.p2.io/version_id')

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

        # ── Versioning: GET a specific historical version ─────────────────────
        requested_version_id = request.GET.get('versionId')
        if requested_version_id:
            volume = await self.get_volume(request.user, bucket, 'read', object_key=path)
            engine = await self.get_engine(volume)
            from p2.s3.versioning import _version_lmdb_key
            lmdb_key = _version_lmdb_key(path, requested_version_id)

            def _get_version_meta():
                with engine.env.begin(db=engine.db) as txn:
                    val = txn.get(lmdb_key)
                    return val

            raw_val = await asyncio.to_thread(_get_version_meta)
            if raw_val is None:
                return HttpResponse(status=404)
            attributes = json.loads(raw_val)
            if attributes.get('blob.p2.io/delete_marker', False):
                response = HttpResponse(status=404)
                response['x-amz-delete-marker'] = 'true'
                response['x-amz-version-id'] = requested_version_id
                return await self._apply_cors(request, response, volume)
            # Serve the blob — reuse the same tiered serving logic below by
            # falling through with `attributes` already set. Build a minimal
            # response directly for simplicity.
            internal_path = attributes.get('internal_path')
            if not internal_path:
                return HttpResponse(status=404)
            from p2.core.storage_path import internal_to_fs
            import os
            fs_path = internal_to_fs(internal_path)
            if not os.path.exists(fs_path):
                return HttpResponse(status=404)
            content_type = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
            total_size = int(attributes.get(ATTR_BLOB_SIZE_BYTES, 0))
            etag = attributes.get('blob.p2.io/hash/md5', '')
            data = await asyncio.to_thread(lambda: open(fs_path, 'rb').read())
            response = HttpResponse(data, content_type=content_type, status=200)
            response['Content-Length'] = total_size
            response['ETag'] = f'"{etag}"' if etag else ''
            response['x-amz-version-id'] = requested_version_id
            return await self._apply_cors(request, response, volume)
        # ─────────────────────────────────────────────────────────────────────

        volume = await self.get_volume(request.user, bucket, 'read', object_key=path)

        attributes = get_cached_metadata(volume.uuid.hex, path)

        if attributes is None:
            engine = await self.get_engine(volume)
            metadata_json = engine.get(path)
            if not metadata_json:
                from p2.s3.versioning import list_versions
                versions = await list_versions(engine, prefix=path, max_keys=1)
                if versions and versions[0]['key'] == path and versions[0]['is_delete_marker']:
                    response = HttpResponse(status=404)
                    response['x-amz-delete-marker'] = 'true'
                    response['x-amz-version-id'] = versions[0]['version_id']
                    return await self._apply_cors(request, response, volume)
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

        internal_path = attributes.get('internal_path', f"/internal-storage/volumes/{volume.uuid.hex}{path}")
        from p2.core.storage_path import internal_to_fs
        fs_path = internal_to_fs(internal_path)

        # Range request support (RFC 7233)
        range_header = request.META.get('HTTP_RANGE')
        if range_header and total_size > 0:
            last_mod = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
            etag_str = f'"{etag}"' if etag else None
            return await self._range_response(
                request, fs_path, content_type, total_size,
                etag_str, last_mod, range_header, volume,
            )

        use_accel_redirect = getattr(settings, 'USE_X_ACCEL_REDIRECT', False)
        if use_accel_redirect and request.META.get('HTTP_X_REAL_IP'):
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
            if attributes.get('blob.p2.io/version_id'):
                response['x-amz-version-id'] = attributes['blob.p2.io/version_id']
            return await self._apply_cors(request, response, volume)
        else:
            # Optimized file serving — fadvise + mmap/pread/streaming
            from p2.s3.fileio import (
                fadvise_random, fadvise_sequential, mmap_read,
                open_noatime, read_file_optimized,
            )
            import os

            SMALL_FILE_MAX = 64 * 1024
            MEDIUM_FILE_MAX = 4 * 1024 * 1024
            STREAM_CHUNK_SIZE = 4 * 1024 * 1024

            if not os.path.exists(fs_path):
                return HttpResponse(status=404)

            if total_size <= SMALL_FILE_MAX:
                # Small file: mmap read (zero syscall overhead)
                try:
                    data = await asyncio.to_thread(mmap_read, fs_path)
                    response = HttpResponse(data, content_type=content_type, status=200)
                except OSError:
                    return HttpResponse(status=404)
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
                try:
                    data = await asyncio.to_thread(_read_medium)
                    response = HttpResponse(data, content_type=content_type, status=200)
                except OSError:
                    return HttpResponse(status=404)
            else:
                # Large file: streaming with fadvise(SEQUENTIAL) + buffered read
                def _stream_large():
                    fd = open_noatime(fs_path)
                    fadvise_sequential(fd)
                    return fd

                fd = await asyncio.to_thread(_stream_large)

                async def _file_stream():
                    try:
                        while True:
                            chunk = await asyncio.to_thread(os.read, fd, STREAM_CHUNK_SIZE)
                            if not chunk:
                                break
                            yield chunk
                    finally:
                        os.close(fd)

                response = StreamingHttpResponse(_file_stream(), content_type=content_type, status=200)

            response['Content-Length'] = total_size
            last_mod = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
            if last_mod:
                response['Last-Modified'] = last_mod
            response['ETag'] = etag
            response['Accept-Ranges'] = 'bytes'
            if 'response-content-type' in request.GET:
                response['Content-Type'] = request.GET['response-content-type']
            if 'response-content-disposition' in request.GET:
                response['Content-Disposition'] = request.GET['response-content-disposition']
            elif content_type != 'application/octet-stream':
                response['Content-Disposition'] = 'inline'
            if attributes.get('blob.p2.io/version_id'):
                response['x-amz-version-id'] = attributes['blob.p2.io/version_id']
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

        # CRC32C: incremental computation to avoid buffering all chunks
        crc32c_val = None
        _rust_cs = None
        if expected_crc32c:
            try:
                from p2.s3 import p2_s3_checksum as _rust_cs
                crc32c_val = 0  # will be computed incrementally
            except (ImportError, AttributeError):
                _rust_cs = None
                crc32c_val = None

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
            # Safe to call directly: the Rust extension releases the GIL
            # during file I/O via py.allow_threads().
            final_md5, final_sha256 = p2_s3_crypto.write_and_hash_small(fs_path, body)
            md5_digest = binascii.unhexlify(final_md5)
            
            if sha1_hasher:
                sha1_hasher.update(body)
            if expected_crc32:
                crc32_val = binascii.crc32(body, crc32_val)
            if crc32c_val is not None:
                crc32c_val = binascii.crc32(body, crc32c_val) & 0xFFFFFFFF
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
                    if crc32c_val is not None:
                        crc32c_val = binascii.crc32(body, crc32c_val) & 0xFFFFFFFF
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
                        if crc32c_val is not None:
                            crc32c_val = binascii.crc32(chunk, crc32c_val) & 0xFFFFFFFF

            md5_digest = md5_hasher.digest()
            final_md5 = md5_hasher.hexdigest()
            final_sha256 = sha256_hasher.hexdigest()

        expected_md5 = request.META.get('HTTP_CONTENT_MD5')
        if expected_md5:
            computed_md5_b64 = base64.b64encode(md5_digest).decode('ascii')
            if computed_md5_b64 != expected_md5:
                raise AWSBadDigest

        expected_content_sha256 = request.META.get('HTTP_X_AMZ_CONTENT_SHA256')
        if (
            expected_content_sha256
            and expected_content_sha256 != 'UNSIGNED-PAYLOAD'
            and not expected_content_sha256.startswith('STREAMING-')
            and expected_content_sha256 != final_sha256
        ):
            from p2.s3.errors import AWSContentSignatureMismatch
            raise AWSContentSignatureMismatch

        crc32_b64 = None
        if expected_crc32:
            crc32_b64 = base64.b64encode(
                (crc32_val & 0xFFFFFFFF).to_bytes(4, byteorder='big', signed=False)
            ).decode('ascii')

        sha1_b64 = None
        if sha1_hasher:
            sha1_b64 = base64.b64encode(sha1_hasher.digest()).decode('ascii')

        crc32c_b64 = None
        if expected_crc32c and _rust_cs is not None and crc32c_val is not None:
            # Convert incremental CRC32C to base64
            crc32c_b64 = base64.b64encode(
                (crc32c_val & 0xFFFFFFFF).to_bytes(4, byteorder='big', signed=False)
            ).decode("ascii")

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

        # ── Versioning: archive old content BEFORE overwriting ────────────────
        # Guard is a single dict-lookup on the already-cached volume object —
        # zero overhead for non-versioned buckets.
        bucket_versioning = (volume.tags or {}).get('versioning') == 'true'
        new_version_id = None
        if bucket_versioning:
            from p2.s3.versioning import archive_version, new_version_id as _new_vid
            if existing_metadata_json and not existing_attr.get(ATTR_BLOB_IS_FOLDER, False):
                # Fire-and-forget as a background task: archive is non-blocking
                # for the response but we still await it to keep ordering.
                await archive_version(engine, path, existing_metadata_json)
            new_version_id = _new_vid()
        # ─────────────────────────────────────────────────────────────────────

        now_ts = str(now())
        metadata_payload = {
            ATTR_BLOB_MIME: client_ct,
            ATTR_BLOB_SIZE_BYTES: str(blob_size),
            ATTR_BLOB_IS_FOLDER: False,
            ATTR_BLOB_STAT_MTIME: now_ts,
            ATTR_BLOB_STAT_CTIME: now_ts,
            'blob.p2.io/hash/md5': final_md5,
            'blob.p2.io/hash/sha256': final_sha256,
            'internal_path': internal_path,
        }
        if new_version_id:
            metadata_payload['blob.p2.io/version_id'] = new_version_id

        metadata_json = json.dumps(metadata_payload)
        from p2.s3.meta_write import write_metadata
        await write_metadata(engine, path, metadata_json)

        if new_version_id:
            from p2.s3.versioning import _version_lmdb_key
            lmdb_key = _version_lmdb_key(path, new_version_id)
            await asyncio.to_thread(engine.put_raw, lmdb_key, metadata_json.encode('utf-8'))

        # Invalidate metadata cache after write
        from p2.s3.cache import invalidate_metadata
        invalidate_metadata(volume.uuid.hex, path)
        if existing_metadata_json:
            from p2.s3.cache import invalidate_volume_global
            invalidate_volume_global(volume.name)
        if existing_metadata_json and not bucket_versioning:
            await cleanup_replaced_payload(existing_attr.get('internal_path'), internal_path)
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
        if new_version_id:
            response['x-amz-version-id'] = new_version_id
        response['X-P2-Put-FastPath'] = '1' if small_put_fast_path else '0'
        response['X-P2-Put-MetaQueue'] = '1' if getattr(settings, 'S3_METADATA_WRITE_QUEUE_ENABLED', False) else '0'
        return await self._apply_cors(request, response, volume)

    async def delete(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectDELETE.html"""
        await self._check_presigned(request, bucket, path)
        volume = await self.get_volume(request.user, bucket, 'delete')

        engine = await self.get_engine(volume)

        # ── Versioning: delete specific version or place a delete marker ──────
        bucket_versioning = (volume.tags or {}).get('versioning') == 'true'
        requested_version_id = request.GET.get('versionId')

        if bucket_versioning and requested_version_id:
            # Permanently delete a specific historical version.
            from p2.s3.versioning import delete_specific_version
            found = await delete_specific_version(engine, path, requested_version_id)
            response = HttpResponse(status=204)
            if found:
                response['x-amz-version-id'] = requested_version_id
            return response

        if bucket_versioning and not requested_version_id:
            # Place a delete marker — do NOT remove the actual data.
            from p2.s3.versioning import write_delete_marker
            from django.utils.timezone import now
            marker_vid = await write_delete_marker(engine, path, str(now()))
            from p2.s3.cache import invalidate_metadata
            invalidate_metadata(volume.uuid.hex, path)
            from p2.s3.cache import invalidate_volume_global
            invalidate_volume_global(volume.name)
            response = HttpResponse(status=204)
            response['x-amz-version-id'] = marker_vid
            response['x-amz-delete-marker'] = 'true'
            return response
        # ─────────────────────────────────────────────────────────────────────

        # Non-versioned (original) path — unchanged.
        metadata_json = await asyncio.to_thread(engine.get, path)

        if metadata_json:
            import os
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
            from p2.s3.cache import invalidate_metadata, invalidate_volume_global
            invalidate_metadata(volume.uuid.hex, path)
            invalidate_volume_global(volume.name)
            from p2.core.volume_stats import adjust_volume_stats
            await adjust_volume_stats(volume, object_delta=object_delta, bytes_delta=bytes_delta)

        return HttpResponse(status=204)

    # -------------------------------------------------------------------------
    # Range requests
    # -------------------------------------------------------------------------

    async def _range_response(self, request, fs_path, content_type, total_size, etag, last_mod, range_header, volume, origin=''):
        """Handle Range: bytes=X-Y requests (RFC 7233)."""
        try:
            unit, ranges = range_header.split('=', 1)
            if unit.strip() != 'bytes':
                raise ValueError
            start_str, end_str = ranges.strip().split('-', 1)
            start = int(start_str) if start_str else None
            end = int(end_str) if end_str else None
        except (ValueError, AttributeError):
            return HttpResponse(status=416)

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
            from p2.s3.fileio import fadvise_random, open_noatime
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
            yield data

        response = StreamingHttpResponse(_ranged_stream(), content_type=content_type, status=206)
        response['Content-Length'] = length
        response['Content-Range'] = f'bytes {start}-{end}/{total_size}'
        response['Accept-Ranges'] = 'bytes'
        if etag:
            response['ETag'] = etag
        if last_mod:
            response['Last-Modified'] = last_mod
        return await self._apply_cors(request, response, volume)

    # -------------------------------------------------------------------------
    # Copy object
    # -------------------------------------------------------------------------

    async def _copy_object(self, request, dest_bucket: str, dest_path: str, copy_source: str):
        """PUT with x-amz-copy-source — copy blob within or across volumes."""
        try:
            import asyncio
            import json
            import os
            import shutil
            import urllib.parse
            import uuid
            from django.utils.timezone import now

            copy_source = urllib.parse.unquote(copy_source).lstrip('/')
            
            parts = copy_source.split('/', 1)
            if len(parts) != 2:
                return HttpResponse(status=400)
                
            src_bucket, src_path = parts
            
            src_version_id = None
            if '?' in src_path:
                src_path_only, query = src_path.split('?', 1)
                src_q = urllib.parse.parse_qs(query)
                src_version_id = src_q.get('versionId', [None])[0]
                src_path = src_path_only
            
            src_volume = await self.get_volume(request.user, src_bucket, 'read')
            dest_volume = await self.get_volume(request.user, dest_bucket, 'write')
            
            src_engine = await self.get_engine(src_volume)
            dest_engine = await self.get_engine(dest_volume)
            
            if src_version_id:
                from p2.s3.versioning import _version_lmdb_key
                lmdb_key = _version_lmdb_key(src_path, src_version_id)
                src_json = await asyncio.to_thread(src_engine.get_raw, lmdb_key)
            else:
                src_json = await asyncio.to_thread(src_engine.get, src_path)

            if not src_json:
                return HttpResponse(status=404)
                
            if isinstance(src_json, bytes):
                src_json = src_json.decode('utf-8')
                
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
                LOGGER.exception("Failed to copy physical file")
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

            dest_versioning = (dest_volume.tags or {}).get('versioning') == 'true'
            new_version_id = None
            if dest_versioning:
                from p2.s3.versioning import archive_version, new_version_id as _new_vid
                if existing_dest_json:
                    await archive_version(dest_engine, dest_path, existing_dest_json)
                new_version_id = _new_vid()
                dest_attr['blob.p2.io/version_id'] = new_version_id
            else:
                dest_attr.pop('blob.p2.io/version_id', None)

            dest_json = json.dumps(dest_attr)
            await asyncio.to_thread(dest_engine.put, dest_path, dest_json)
            if new_version_id:
                from p2.s3.versioning import _version_lmdb_key
                lmdb_key = _version_lmdb_key(dest_path, new_version_id)
                await asyncio.to_thread(dest_engine.put_raw, lmdb_key, dest_json.encode('utf-8'))

            from p2.core.volume_stats import adjust_volume_stats
            await adjust_volume_stats(
                dest_volume,
                object_delta=0 if existing_dest_counted else 1,
                bytes_delta=int(dest_attr.get(ATTR_BLOB_SIZE_BYTES, 0) or 0) - existing_dest_size,
            )
            from p2.s3.cache import invalidate_metadata, invalidate_volume_global
            invalidate_metadata(dest_volume.uuid.hex, dest_path)
            if existing_dest_json:
                invalidate_volume_global(dest_volume.name)
            if existing_dest_json and not dest_versioning:
                await cleanup_replaced_payload(
                    existing_dest_attr.get('internal_path'),
                    dest_internal_path,
                )
            
            root = ElementTree.Element("{%s}CopyObjectResult" % XML_NAMESPACE)
            ElementTree.SubElement(root, "LastModified").text = dest_attr[ATTR_BLOB_STAT_MTIME]
            etag = dest_attr.get('blob.p2.io/hash/md5', '')
            if etag:
                ElementTree.SubElement(root, "ETag").text = f'"{etag}"'
                
            response = XMLResponse(root)
            if new_version_id:
                response['x-amz-version-id'] = new_version_id
            actual_src_vid = src_attr.get('blob.p2.io/version_id') or src_version_id
            if actual_src_vid:
                response['x-amz-copy-source-version-id'] = actual_src_vid
            return response
        except Exception as e:
            LOGGER.exception("Unexpected exception in _copy_object")
            return HttpResponse(status=500)

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
        invalidate_metadata(volume.uuid.hex, path)
        from p2.s3.cache import invalidate_volume_global
        invalidate_volume_global(volume.name)
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
            invalidate_metadata(volume.uuid.hex, path)
            from p2.s3.cache import invalidate_volume_global
            invalidate_volume_global(volume.name)
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
        invalidate_metadata(volume.uuid.hex, path)
        from p2.s3.cache import invalidate_volume_global
        invalidate_volume_global(volume.name)
        return HttpResponse(status=200)
