"""p2 S3 Object views"""
import asyncio
import logging

from asgiref.sync import sync_to_async
from django.db import IntegrityError
from django.http.response import HttpResponse, StreamingHttpResponse

from p2.core.constants import (ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME,
                               ATTR_BLOB_SIZE_BYTES)
from p2.core.models import Blob
from p2.core.signals import BLOB_PRE_SAVE
from p2.core.storages.base import AsyncStorageController
from p2.s3.views.common import S3View
from p2.s3.views.multipart import MultipartUploadView

LOGGER = logging.getLogger(__name__)


async def _request_body_chunks(request):
    """Async generator yielding the request body as a single chunk."""
    body = request.body  # already buffered by Django ASGI handler
    if body:
        yield body


async def _blob_read_stream(blob):
    """Async generator streaming blob data from storage."""
    controller = blob.volume.storage.controller
    if isinstance(controller, AsyncStorageController):
        async for chunk in controller.get_read_stream(blob):
            yield chunk
    else:
        # Sync fallback: read in thread pool to avoid blocking the event loop.
        data = await asyncio.to_thread(blob.read)
        if data:
            yield data


def _fire_pre_save(blob):
    """Send BLOB_PRE_SAVE signal synchronously (quota check)."""
    BLOB_PRE_SAVE.send(sender=Blob, blob=blob)


class ObjectView(S3View):
    """Object related views — all handlers are async. Requirements: 2.1, 2.2, 2.3, 2.6, 4.2, 8.3"""

    async def head(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectHEAD.html"""
        volume = await self.get_volume(request.user, bucket, 'read')
        blob = await Blob.objects.filter(path=path, volume=volume).afirst()
        if blob is None:
            return HttpResponse(status=404)
        response = HttpResponse(status=200)
        response['Content-Length'] = blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
        response['Content-Type'] = blob.attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        return response

    async def get(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectGET.html"""
        volume = await self.get_volume(request.user, bucket, 'read')
        blob = await self.get_blob(volume, path)
        content_type = blob.attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        response = StreamingHttpResponse(_blob_read_stream(blob), content_type=content_type)
        response['Content-Length'] = blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
        return response

    async def post(self, request, bucket, path):
        """POST is handled by MultipartUploadView."""
        return await MultipartUploadView().dispatch(request, bucket, path)

    async def put(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPUT.html"""
        if 'uploadId' in request.GET:
            return await MultipartUploadView().dispatch(request, bucket, path)

        volume = await self.get_volume(request.user, bucket, 'write')
        created = False
        try:
            blob, created = await Blob.objects.aget_or_create(path=path, volume=volume)
        except IntegrityError:
            # Race condition: another request created the blob between the GET and INSERT.
            blob = await Blob.objects.aget(path=path, volume=volume)
        if created and request.body == b'':
            blob.attributes[ATTR_BLOB_IS_FOLDER] = True

        # Sync pre-save quota check — must block the write. Req 2.6, 8.3.
        await sync_to_async(_fire_pre_save)(blob)

        controller = volume.storage.controller
        if isinstance(controller, AsyncStorageController):
            await controller.commit(blob, _request_body_chunks(request))
        else:
            body = request.body
            await asyncio.to_thread(blob.write, body)

        # blob.save() publishes BLOB_POST_SAVE and BLOB_PAYLOAD_UPDATED events internally.
        await sync_to_async(blob.save)()
        return HttpResponse(status=200)

    async def delete(self, request, bucket, path):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectDELETE.html"""
        volume = await self.get_volume(request.user, bucket, 'delete')
        blob = await Blob.objects.filter(path=path, volume=volume).afirst()
        if blob is not None:
            await blob.adelete()
        return HttpResponse(status=204)
