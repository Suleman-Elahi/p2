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
from p2.s3.checksum import verify_request_checksum
from p2.s3.cors import apply_cors_headers, find_matching_rule, get_cors_rules
from p2.s3.errors import AWSAccessDenied, AWSBadDigest, AWSNoSuchKey
from p2.s3.http import XMLResponse
from p2.s3.presign import validate_presigned_token
from p2.s3.views.common import S3View
from p2.s3.views.multipart import MultipartUploadView
from p2.s3.utils import decode_aws_chunked
from p2.s3.cache import get_cached_metadata, set_cached_metadata, invalidate_metadata
import json
import asyncio
from django.conf import settings
from django.http import StreamingHttpResponse

USE_ACCEL_REDIRECT = getattr(settings, 'USE_X_ACCEL_REDIRECT', False)


def _format_http_date(mtime_str: str) -> str:
    """Convert stored mtime string to RFC 7231 HTTP date format."""
    if not mtime_str:
        return ''
    dt = parse_datetime(mtime_str)
    if dt is None:
        return mtime_str
    return format_datetime(dt, usegmt=True)

from p2.s3 import p2_s3_meta

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
        response['Last-Modified'] = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
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
        fs_path = internal_path.replace('/internal-storage/', '/storage/')

        if USE_ACCEL_REDIRECT:
            # X-Accel-Redirect to Nginx — zero-copy sendfile path.
            # Do NOT set Content-Length here: the body is empty from uvicorn's
            # perspective. Nginx reads the file and sets the correct length itself.
            response = HttpResponse()
            response['X-Accel-Redirect'] = internal_path
            response['Content-Type'] = content_type
            response['Last-Modified'] = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
            response['ETag'] = etag
            response['Accept-Ranges'] = 'bytes'
            if 'response-content-type' in request.GET:
                response['Content-Type'] = request.GET['response-content-type']
            if 'response-content-disposition' in request.GET:
                response['Content-Disposition'] = request.GET['response-content-disposition']
            return await self._apply_cors(request, response, volume)
        else:
            # Direct Python fallback (no Nginx)
            if total_size <= 1048576:  # 1MB
                def _read_all():
                    with open(fs_path, 'rb') as f:
                        return f.read()
                data = await asyncio.to_thread(_read_all)
                response = HttpResponse(data, content_type=content_type, status=200)
            else:
                import aiofiles

                async def _stream():
                    async with aiofiles.open(fs_path, 'rb') as f:
                        chunk = await f.read(1 << 20)  # 1 MB
                        while chunk:
                            yield chunk
                            chunk = await f.read(1 << 20)

                response = StreamingHttpResponse(_stream(), content_type=content_type, status=200)

            response['Content-Length'] = total_size
            response['Last-Modified'] = _format_http_date(attributes.get(ATTR_BLOB_STAT_MTIME, ''))
            response['ETag'] = etag
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
        import os
        import uuid
        import json
        import aiofiles
        from django.utils.timezone import now
        
        engine = await self.get_engine(volume)
        metadata_json = engine.get(path)
        existing_attributes = json.loads(metadata_json) if metadata_json else {}

        # Honour Content-Type from the client
        client_ct = request.META.get('CONTENT_TYPE', 'application/octet-stream')

        # Read body — decode aws-chunked if present
        # AWS SDKs use x-amz-decoded-content-length when sending chunked payloads
        import asyncio
        body = await asyncio.to_thread(request.read)
        content_encoding = request.META.get('HTTP_CONTENT_ENCODING', '')
        decoded_length = request.META.get('HTTP_X_AMZ_DECODED_CONTENT_LENGTH')

        # Detect aws-chunked: either explicit header or decoded-length hint
        if 'aws-chunked' in content_encoding or decoded_length:
            body = decode_aws_chunked(body)
            LOGGER.debug("PUT %s: decoded aws-chunked %d -> %d bytes", path, len(body) + 175, len(body))

        # Streaming high-throughput write
        blob_uuid = uuid.uuid4().hex
        dir_path = os.path.join("/storage/volumes", volume.uuid.hex, blob_uuid[0:2], blob_uuid[2:4])
        os.makedirs(dir_path, exist_ok=True)
        fs_path = os.path.join(dir_path, blob_uuid)
        internal_path = f"/internal-storage/volumes/{volume.uuid.hex}/{blob_uuid[0:2]}/{blob_uuid[2:4]}/{blob_uuid}"

        # Use Rust checksum extension for inline hashing (releases GIL, ~3x faster)
        try:
            from p2.s3 import p2_s3_checksum as _rust_cs
            final_md5 = _rust_cs.md5_hex(body)
            final_sha256 = _rust_cs.sha256_hex(body)
        except (ImportError, AttributeError):
            import hashlib
            final_md5 = hashlib.md5(body).hexdigest()
            final_sha256 = hashlib.sha256(body).hexdigest()

        blob_size = len(body)

        async with aiofiles.open(fs_path, 'wb') as f:
            await f.write(body)

        # Update and save attributes in redb
        existing_attributes.update({
            ATTR_BLOB_MIME: client_ct,
            ATTR_BLOB_SIZE_BYTES: str(blob_size),
            ATTR_BLOB_IS_FOLDER: False,
            ATTR_BLOB_STAT_MTIME: str(now()),
            'blob.p2.io/hash/md5': final_md5,
            'blob.p2.io/hash/sha256': final_sha256,
            'internal_path': internal_path
        })
        
        if not metadata_json:
            existing_attributes[ATTR_BLOB_STAT_CTIME] = str(now())

        engine.put(path, json.dumps(existing_attributes))
        
        # Invalidate metadata cache after write
        from p2.s3.cache import invalidate_metadata
        invalidate_metadata(volume.uuid.hex, path)

        # Publish event to Dragonfly for background processing (webhooks, EXIF, etc.)
        try:
            from p2.core.events import STREAM_BLOB_POST_SAVE, make_event, publish_event
            event = make_event(
                blob_uuid=blob_uuid,
                volume_uuid=volume.uuid.hex,
                event_type="blob_post_save"
            )
            event['blob_path'] = path
            event['mime'] = client_ct
            await publish_event(STREAM_BLOB_POST_SAVE, event)
        except Exception as e:
            LOGGER.warning("Failed to publish blob event: %s", e)

        response = HttpResponse(status=200)
        response['ETag'] = f'"{final_md5}"'
        return await self._apply_cors(request, response, volume)

    async def delete(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectDELETE.html"""
        await self._check_presigned(request, bucket, path)
        volume = await self.get_volume(request.user, bucket, 'delete')
        
        engine = await self.get_engine(volume)
        metadata_json = engine.get(path)
        
        if metadata_json:
            import json, os
            attributes = json.loads(metadata_json)
            internal_path = attributes.get('internal_path')
            if internal_path:
                fs_path = internal_path.replace('/internal-storage/', '/storage/')
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
            engine.delete(path)
            
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
        
        src_json = src_engine.get(src_path)
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
        dir_path = os.path.join("/storage", dest_volume.uuid.hex, blob_uuid[0:2], blob_uuid[2:4])
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
        
        dest_engine.put(dest_path, json.dumps(dest_attr))
        
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
