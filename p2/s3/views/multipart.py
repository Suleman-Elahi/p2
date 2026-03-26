"""p2 S3 Multipart Upload views"""
import asyncio
import logging
from time import time
from uuid import uuid4
from xml.etree import ElementTree

from arq import create_pool
from arq.connections import RedisSettings
from django.conf import settings
from django.http.response import HttpResponse

from p2.components.expire.constants import TAG_EXPIRE_DATE
from p2.core.acl import VolumeACL
from p2.core.constants import ATTR_BLOB_HASH_MD5, ATTR_BLOB_SIZE_BYTES
from p2.core.models import Blob
from p2.core.prefix_helper import make_absolute_path
from p2.s3.constants import (TAG_S3_MULTIPART_BLOB_PART,
                             TAG_S3_MULTIPART_BLOB_TARGET_BLOB,
                             TAG_S3_MULTIPART_BLOB_UPLOAD_ID, XML_NAMESPACE)
from p2.core.storages.base import AsyncStorageController
from p2.s3.http import XMLResponse
from p2.s3.views.common import S3View

logger = logging.getLogger(__name__)

DEFAULT_BLOB_EXPIRY = 86400


class MultipartUploadView(S3View):
    """Multipart-Object related views -- all handlers are async."""

    async def post(self, request, bucket, path):
        volume = await self.get_volume(request.user, bucket, 'write')
        if 'uploadId' in request.GET:
            return await self.post_handle_mp_complete(request, volume, path)
        return await self.post_handle_mp_initiate(request, volume, path)

    async def post_handle_mp_complete(self, request, volume, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadComplete.html"""
        upload_id = request.GET.get('uploadId')
        exists = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).aexists()
        if not exists:
            return HttpResponse(status=404)

        try:
            pool = await create_pool(RedisSettings.from_dsn(settings.ARQ_REDIS_URL))
            try:
                await pool.enqueue_job(
                    "complete_multipart",
                    upload_id,
                    request.user.pk,
                    str(volume.pk),
                    path,
                )
            finally:
                await pool.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.error("post_handle_mp_complete: failed to enqueue: %s", exc)
            return HttpResponse(status=500)

        return HttpResponse(status=200)

    async def post_handle_mp_initiate(self, request, volume, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadInitiate.html"""
        existing = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).afirst()

        root = ElementTree.Element("{%s}InitiateMultipartUploadResult" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Bucket").text = volume.name
        ElementTree.SubElement(root, "Key").text = path.lstrip('/')
        upload_id = uuid4().hex

        if existing is not None:
            blob = existing
        else:
            blob = await Blob.objects.acreate(
                path=make_absolute_path("/%s_%s/part_%d" % (path, upload_id, 1)),
                volume=volume,
                tags={
                    TAG_S3_MULTIPART_BLOB_PART: 1,
                    TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
                    TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
                    TAG_EXPIRE_DATE: time() + DEFAULT_BLOB_EXPIRY,
                }
            )
            await VolumeACL.objects.aupdate_or_create(
                volume=volume,
                user=request.user,
                defaults={'permissions': ['read', 'write']},
            )

        ElementTree.SubElement(root, "UploadId").text = blob.tags[TAG_S3_MULTIPART_BLOB_UPLOAD_ID]
        return XMLResponse(root)

    async def put(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadUploadPart.html"""
        volume = await self.get_volume(request.user, bucket, 'write')
        upload_id = request.GET.get('uploadId')
        part_number = int(request.GET.get('partNumber'))

        exists = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).aexists()
        if not exists:
            return HttpResponse(status=404)

        # UploadPartCopy: read data from source blob instead of request body
        copy_source = request.META.get('HTTP_X_AMZ_COPY_SOURCE')
        if copy_source:
            copy_source = copy_source.lstrip('/')
            src_bucket, src_key = copy_source.split('/', 1)
            src_key = '/' + src_key
            src_volume = await self.get_volume(request.user, src_bucket, 'read')
            src_blob = await Blob.objects.filter(
                path=src_key, volume=src_volume,
            ).select_related('volume__storage').afirst()
            if src_blob is None:
                return HttpResponse(status=404)
            controller = src_volume.storage.controller
            if isinstance(controller, AsyncStorageController):
                chunks = []
                async for chunk in controller.get_read_stream(src_blob):
                    chunks.append(chunk)
                data = b''.join(chunks)
            else:
                data = await asyncio.to_thread(src_blob.read)
        else:
            data = request.body

        blob = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_PART: part_number,
            'volume': volume,
        }).afirst()

        if blob is None:
            blob = await Blob.objects.acreate(
                path=make_absolute_path("/%s_%s/part_%d" % (path, upload_id, part_number)),
                volume=volume,
                tags={
                    TAG_S3_MULTIPART_BLOB_PART: part_number,
                    TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
                    TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
                    TAG_EXPIRE_DATE: time() + DEFAULT_BLOB_EXPIRY,
                }
            )

        blob.write(data)
        await blob.asave()

        response = HttpResponse(status=200)
        response['ETag'] = blob.attributes.get(ATTR_BLOB_HASH_MD5)
        if copy_source:
            response['x-amz-copy-source-version-id'] = ''
        return response

    async def delete(self, request, bucket, path):
        """AbortMultipartUpload — DELETE /<bucket>/<key>?uploadId=...
        https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadAbort.html"""
        upload_id = request.GET.get('uploadId')
        if not upload_id:
            return HttpResponse(status=400)
        volume = await self.get_volume(request.user, bucket, 'write')
        # Delete all part blobs for this upload
        async for part in Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).aiterator():
            await part.adelete()
        return HttpResponse(status=204)

    async def get(self, request, bucket, path):
        """ListParts — GET /<bucket>/<key>?uploadId=...
        https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadListParts.html"""
        upload_id = request.GET.get('uploadId')
        if not upload_id:
            return HttpResponse(status=400)
        volume = await self.get_volume(request.user, bucket, 'read')
        max_parts = int(request.GET.get('max-parts', 1000))
        part_number_marker = int(request.GET.get('part-number-marker', 0))

        root = ElementTree.Element("{%s}ListPartsResult" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Bucket").text = bucket
        ElementTree.SubElement(root, "Key").text = path.lstrip('/')
        ElementTree.SubElement(root, "UploadId").text = upload_id

        parts = []
        async for blob in Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).order_by('path').aiterator():
            part_num = blob.tags.get(TAG_S3_MULTIPART_BLOB_PART, 0)
            if int(part_num) > part_number_marker:
                parts.append(blob)

        is_truncated = len(parts) > max_parts
        parts = parts[:max_parts]

        ElementTree.SubElement(root, "IsTruncated").text = str(is_truncated).lower()
        ElementTree.SubElement(root, "MaxParts").text = str(max_parts)

        for blob in parts:
            part_el = ElementTree.SubElement(root, "Part")
            ElementTree.SubElement(part_el, "PartNumber").text = str(
                blob.tags.get(TAG_S3_MULTIPART_BLOB_PART, 0))
            ElementTree.SubElement(part_el, "ETag").text = blob.attributes.get(ATTR_BLOB_HASH_MD5, '')
            ElementTree.SubElement(part_el, "Size").text = str(
                blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0))

        return XMLResponse(root)


logger = logging.getLogger(__name__)

DEFAULT_BLOB_EXPIRY = 86400


class MultipartUploadView(S3View):
    """Multipart-Object related views -- all handlers are async. Requirements: 2.1, 2.4"""

    ## HTTP Method handlers

    async def post(self, request, bucket, path):
        """Post handler"""
        volume = await self.get_volume(request.user, bucket, 'write')
        if 'uploadId' in request.GET:
            return await self.post_handle_mp_complete(request, volume, path)
        return await self.post_handle_mp_initiate(request, volume, path)

    ## API Handlers

    async def post_handle_mp_complete(self, request, volume, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadComplete.html"""
        upload_id = request.GET.get('uploadId')
        # Ensure multipart upload has started -- verify at least one part blob exists.
        exists = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).aexists()
        if not exists:
            return HttpResponse(status=404)

        try:
            pool = await create_pool(RedisSettings.from_dsn(settings.ARQ_REDIS_URL))
            try:
                await pool.enqueue_job(
                    "complete_multipart",
                    upload_id,
                    request.user.pk,
                    str(volume.pk),
                    path,
                )
            finally:
                await pool.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.error("post_handle_mp_complete: failed to enqueue complete_multipart: %s", exc)
            return HttpResponse(status=500)

        return HttpResponse(status=200)

    async def post_handle_mp_initiate(self, request, volume, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadInitiate.html"""
        # Check if an existing multipart upload exists for this path.
        existing = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).afirst()

        root = ElementTree.Element("{%s}InitiateMultipartUploadResult" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Bucket").text = volume.name
        ElementTree.SubElement(root, "Key").text = path.lstrip('/')
        upload_id = uuid4().hex

        if existing is not None:
            blob = existing
        else:
            blob = await Blob.objects.acreate(
                path=make_absolute_path("/%s_%s/part_%d" % (path, upload_id, 1)),
                volume=volume,
                tags={
                    TAG_S3_MULTIPART_BLOB_PART: 1,
                    TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
                    TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
                    TAG_EXPIRE_DATE: time() + DEFAULT_BLOB_EXPIRY,
                }
            )
            await VolumeACL.objects.aupdate_or_create(
                volume=volume,
                user=request.user,
                defaults={'permissions': ['read', 'write']},
            )

        ElementTree.SubElement(root, "UploadId").text = blob.tags[TAG_S3_MULTIPART_BLOB_UPLOAD_ID]
        return XMLResponse(root)

    async def put(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/mpUploadUploadPart.html"""
        volume = await self.get_volume(request.user, bucket, 'write')
        upload_id = request.GET.get('uploadId')
        part_number = int(request.GET.get('partNumber'))

        # Ensure multipart upload has started -- verify the upload ID exists.
        exists = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'volume': volume,
        }).aexists()
        if not exists:
            return HttpResponse(status=404)

        # Create new upload part, or reuse existing part and overwrite data.
        blob = await Blob.objects.filter(**{
            'tags__%s' % TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
            'tags__%s' % TAG_S3_MULTIPART_BLOB_PART: part_number,
            'volume': volume,
        }).afirst()

        if blob is None:
            blob = await Blob.objects.acreate(
                path=make_absolute_path("/%s_%s/part_%d" % (path, upload_id, part_number)),
                volume=volume,
                tags={
                    TAG_S3_MULTIPART_BLOB_PART: part_number,
                    TAG_S3_MULTIPART_BLOB_TARGET_BLOB: path,
                    TAG_S3_MULTIPART_BLOB_UPLOAD_ID: upload_id,
                    TAG_EXPIRE_DATE: time() + DEFAULT_BLOB_EXPIRY,
                }
            )

        blob.write(request.body)
        await blob.asave()

        response = HttpResponse(status=200)
        response['ETag'] = blob.attributes.get(ATTR_BLOB_HASH_MD5)
        return response
