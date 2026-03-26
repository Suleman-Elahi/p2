"""p2 S3 Object views"""
import asyncio
import logging
from xml.etree import ElementTree

from asgiref.sync import sync_to_async
from django.db import IntegrityError
from django.http.response import HttpResponse, StreamingHttpResponse

from p2.core.acl import VolumeACL, has_volume_permission
from p2.core.constants import (ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME,
                               ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_STAT_MTIME)
from p2.core.models import Blob
from p2.core.signals import BLOB_PRE_SAVE
from p2.core.storages.base import AsyncStorageController
from p2.s3.constants import (TAG_S3_ACL, TAG_S3_USER_TAG_PREFIX,
                             XML_NAMESPACE)
from p2.s3.checksum import verify_request_checksum
from p2.s3.cors import apply_cors_headers, find_matching_rule, get_cors_rules
from p2.s3.errors import AWSAccessDenied, AWSBadDigest, AWSNoSuchKey
from p2.s3.http import XMLResponse
from p2.s3.presign import validate_presigned_token
from p2.s3.views.common import S3View
from p2.s3.views.multipart import MultipartUploadView

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


async def _request_body_chunks(request):
    body = request.body
    if body:
        yield body


async def _blob_read_stream(blob):
    controller = blob.volume.storage.controller
    if isinstance(controller, AsyncStorageController):
        async for chunk in controller.get_read_stream(blob):
            yield chunk
    else:
        data = await asyncio.to_thread(blob.read)
        if data:
            yield data


def _fire_pre_save(blob):
    BLOB_PRE_SAVE.send(sender=Blob, blob=blob)


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


def _user_tags_from_blob(blob: Blob) -> dict:
    """Extract S3 user tags (s3.user/* prefix) from blob.tags."""
    return {
        k[len(TAG_S3_USER_TAG_PREFIX):]: v
        for k, v in blob.tags.items()
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


def _build_acl_xml(blob: Blob, owner_id: str, owner_name: str) -> ElementTree.Element:
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
        validate_presigned_token(token, bucket, path, request.method, max_age=max_age)
        # Mark request as presigned so middleware skips re-auth
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
        await self._check_presigned(request, bucket, path)
        volume = await self.get_volume(request.user, bucket, 'read')
        blob = await Blob.objects.filter(path=path, volume=volume).afirst()
        if blob is None:
            return HttpResponse(status=404)
        response = HttpResponse(status=200)
        response['Content-Length'] = blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
        response['Content-Type'] = blob.attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        response['Last-Modified'] = blob.attributes.get(ATTR_BLOB_STAT_MTIME, '')
        response['ETag'] = blob.attributes.get('blob.p2.io/hash/md5', '')
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

        volume = await self.get_volume(request.user, bucket, 'read')
        blob = await self.get_blob(volume, path)
        content_type = blob.attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        total_size = int(blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0))

        # Range request support
        range_header = request.META.get('HTTP_RANGE', '')
        if range_header:
            return await self._range_response(request, blob, content_type, total_size, range_header, volume)

        etag = blob.attributes.get('blob.p2.io/hash/md5', '')
        # If-None-Match → 304 Not Modified
        if_none_match = request.META.get('HTTP_IF_NONE_MATCH')
        if if_none_match and etag:
            tags = [t.strip().strip('"') for t in if_none_match.split(',')]
            if etag.strip('"') in tags:
                resp = HttpResponse(status=304)
                resp['ETag'] = etag
                return await self._apply_cors(request, resp, volume)
        # If-Modified-Since → 304 Not Modified
        if_mod_since = request.META.get('HTTP_IF_MODIFIED_SINCE')
        if if_mod_since:
            from email.utils import parsedate_to_datetime
            from django.utils.dateparse import parse_datetime
            try:
                threshold = parsedate_to_datetime(if_mod_since)
                mtime = blob.attributes.get(ATTR_BLOB_STAT_MTIME, '')
                if mtime:
                    blob_dt = parse_datetime(str(mtime))
                    if blob_dt and blob_dt <= threshold:
                        resp = HttpResponse(status=304)
                        return await self._apply_cors(request, resp, volume)
            except (ValueError, TypeError):
                pass
        response = StreamingHttpResponse(_blob_read_stream(blob), content_type=content_type)
        response['Content-Length'] = total_size
        response['Last-Modified'] = blob.attributes.get(ATTR_BLOB_STAT_MTIME, '')
        response['ETag'] = etag
        response['Accept-Ranges'] = 'bytes'
        # S3 response override query params
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
        # Check conditional headers against existing object
        existing = await Blob.objects.filter(path=path, volume=volume).afirst()
        cond_resp = _check_conditional_headers(request, existing)
        if cond_resp is not None:
            return cond_resp
        created = False
        try:
            blob, created = await Blob.objects.aget_or_create(path=path, volume=volume)
        except IntegrityError:
            blob = await Blob.objects.aget(path=path, volume=volume)
        if created and request.body == b'':
            blob.attributes[ATTR_BLOB_IS_FOLDER] = True

        # Apply canned ACL from header if present
        canned_acl = request.META.get('HTTP_X_AMZ_ACL')
        if canned_acl and canned_acl in _CANNED_ACL_PERMS:
            blob.tags[TAG_S3_ACL] = canned_acl
            if 'public-read' in canned_acl:
                blob.volume.public_read = True
                await blob.volume.asave(update_fields=['public_read'])

        await sync_to_async(_fire_pre_save)(blob)

        # Honour Content-Type from the client (SDKs send the correct MIME type)
        client_ct = request.META.get('CONTENT_TYPE', '')
        if client_ct and client_ct != 'application/octet-stream':
            blob.attributes[ATTR_BLOB_MIME] = client_ct

        # Verify payload checksum if x-amz-checksum-* header present
        checksum_err = verify_request_checksum(request, request.body)
        if checksum_err:
            raise AWSBadDigest

        controller = volume.storage.controller
        if isinstance(controller, AsyncStorageController):
            await controller.commit(blob, _request_body_chunks(request))
        else:
            body = request.body
            await asyncio.to_thread(blob.write, body)

        await sync_to_async(blob.save)()
        response = HttpResponse(status=200)
        response['ETag'] = blob.attributes.get('blob.p2.io/hash/md5', '')
        return await self._apply_cors(request, response, volume)

    async def delete(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectDELETE.html"""
        if 'tagging' in request.GET:
            return await self._delete_tagging(request, bucket, path)
        # Abort multipart
        if 'uploadId' in request.GET:
            return await MultipartUploadView().dispatch(request, bucket, path)
        volume = await self.get_volume(request.user, bucket, 'delete')
        blob = await Blob.objects.filter(path=path, volume=volume).afirst()
        if blob is not None:
            await blob.adelete()
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
        # copy_source is /src-bucket/src-key or src-bucket/src-key
        copy_source = copy_source.lstrip('/')
        if '/' not in copy_source:
            return HttpResponse(status=400)
        src_bucket, src_key = copy_source.split('/', 1)
        src_key = '/' + src_key

        src_volume = await self.get_volume(request.user, src_bucket, 'read')
        src_blob = await Blob.objects.filter(path=src_key, volume=src_volume).select_related('volume__storage').afirst()
        if src_blob is None:
            raise AWSNoSuchKey

        dest_volume = await self.get_volume(request.user, dest_bucket, 'write')
        # Check conditional headers against existing destination object
        existing_dest = await Blob.objects.filter(path=dest_path, volume=dest_volume).afirst()
        cond_resp = _check_conditional_headers(request, existing_dest)
        if cond_resp is not None:
            return cond_resp

        # Read source data
        src_controller = src_volume.storage.controller
        if isinstance(src_controller, AsyncStorageController):
            chunks = []
            async for chunk in src_controller.get_read_stream(src_blob):
                chunks.append(chunk)
            data = b''.join(chunks)
        else:
            data = await asyncio.to_thread(src_blob.read)

        # Write to destination
        try:
            dest_blob, _ = await Blob.objects.aget_or_create(path=dest_path, volume=dest_volume)
        except IntegrityError:
            dest_blob = await Blob.objects.aget(path=dest_path, volume=dest_volume)

        dest_blob.attributes.update(src_blob.attributes)

        dest_controller = dest_volume.storage.controller

        async def _data_stream():
            yield data

        if isinstance(dest_controller, AsyncStorageController):
            await dest_controller.commit(dest_blob, _data_stream())
        else:
            await asyncio.to_thread(dest_blob.write, data)

        await sync_to_async(dest_blob.save)()

        root = ElementTree.Element("{%s}CopyObjectResult" % XML_NAMESPACE)
        ElementTree.SubElement(root, "LastModified").text = str(dest_blob.attributes.get(ATTR_BLOB_STAT_MTIME, ''))
        ElementTree.SubElement(root, "ETag").text = dest_blob.attributes.get('blob.p2.io/hash/md5', '')
        return XMLResponse(root)

    # -------------------------------------------------------------------------
    # Object tagging
    # -------------------------------------------------------------------------

    async def _get_tagging(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'read')
        blob = await self.get_blob(volume, path)
        tags = _user_tags_from_blob(blob)
        return XMLResponse(_build_tagging_xml(tags))

    async def _put_tagging(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'write')
        blob = await self.get_blob(volume, path)
        new_tags = _parse_tagging_xml(request.body)
        # Remove old user tags, apply new ones
        blob.tags = {k: v for k, v in blob.tags.items() if not k.startswith(TAG_S3_USER_TAG_PREFIX)}
        for k, v in new_tags.items():
            blob.tags[f"{TAG_S3_USER_TAG_PREFIX}{k}"] = v
        await blob.asave(update_fields=['tags'])
        return HttpResponse(status=200)

    async def _delete_tagging(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'write')
        blob = await self.get_blob(volume, path)
        blob.tags = {k: v for k, v in blob.tags.items() if not k.startswith(TAG_S3_USER_TAG_PREFIX)}
        await blob.asave(update_fields=['tags'])
        return HttpResponse(status=204)

    # -------------------------------------------------------------------------
    # Object ACL
    # -------------------------------------------------------------------------

    async def _get_acl(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'read')
        blob = await self.get_blob(volume, path)
        owner_id = str(request.user.pk)
        owner_name = request.user.username
        return XMLResponse(_build_acl_xml(blob, owner_id, owner_name))

    async def _put_acl(self, request, bucket: str, path: str):
        volume = await self.get_volume(request.user, bucket, 'write')
        blob = await self.get_blob(volume, path)
        canned = request.META.get('HTTP_X_AMZ_ACL', 'private')
        if canned not in _CANNED_ACL_PERMS:
            canned = 'private'
        blob.tags[TAG_S3_ACL] = canned
        # Sync public_read flag on volume for public-read ACLs
        if 'public-read' in canned:
            volume.public_read = True
            await volume.asave(update_fields=['public_read'])
        await blob.asave(update_fields=['tags'])
        return HttpResponse(status=200)
