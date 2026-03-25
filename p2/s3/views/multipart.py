"""p2 S3 Multipart Upload views"""
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
from p2.core.constants import ATTR_BLOB_HASH_MD5
from p2.core.models import Blob
from p2.core.prefix_helper import make_absolute_path
from p2.s3.constants import (TAG_S3_MULTIPART_BLOB_PART,
                             TAG_S3_MULTIPART_BLOB_TARGET_BLOB,
                             TAG_S3_MULTIPART_BLOB_UPLOAD_ID, XML_NAMESPACE)
from p2.s3.http import XMLResponse
from p2.s3.views.common import S3View

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
