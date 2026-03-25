"""p2 S3 Bucket-related Views"""
import logging
from xml.etree import ElementTree

from django.http import HttpResponse

from p2.core.acl import VolumeACL
from p2.core.constants import (ATTR_BLOB_HASH_MD5, ATTR_BLOB_IS_FOLDER,
                               ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_STAT_MTIME)
from p2.core.models import Blob, Storage, Volume
from p2.core.prefix_helper import make_absolute_prefix
from p2.s3.constants import (TAG_S3_DEFAULT_STORAGE, TAG_S3_STORAGE_CLASS,
                             XML_NAMESPACE)
from p2.s3.errors import AWSAccessDenied
from p2.s3.http import XMLResponse
from p2.s3.views.common import S3View

LOGGER = logging.getLogger(__name__)


class BucketView(S3View):
    """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTBucketOps.html"""

    async def get(self, request, *args, **kwargs):
        """Boilerplate to pass request to correct handler"""
        if "versioning" in request.GET:
            return self.handler_versioning(request, *args, **kwargs)
        if 'uploads' in request.GET:
            return self.handler_uploads(request, *args, **kwargs)
        return await self.handler_list(request, *args, **kwargs)

    def handler_versioning(self, request, bucket):
        root = ElementTree.Element("{%s}VersioningConfiguration" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Status").text = "Disabled"
        return XMLResponse(root)

    def handler_uploads(self, request, bucket):
        root = ElementTree.Element("{%s}VersioningConfiguration" % XML_NAMESPACE)
        ElementTree.SubElement(root, "Status").text = "Disabled"
        return XMLResponse(root)

    def _etree_for_blob(self, blob):
        content = ElementTree.Element("Contents")
        ElementTree.SubElement(content, "Key").text = blob.path[1:]
        ElementTree.SubElement(content, "LastModified").text = blob.attributes.get(ATTR_BLOB_STAT_MTIME)
        ElementTree.SubElement(content, "ETag").text = blob.attributes.get(ATTR_BLOB_HASH_MD5)
        ElementTree.SubElement(content, "Size").text = str(blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0))
        ElementTree.SubElement(content, "StorageClass").text = \
            blob.volume.storage.controller.tags.get(TAG_S3_STORAGE_CLASS, 'default')
        return content

    async def handler_list(self, request, bucket):
        """Bucket List API Method"""
        root = ElementTree.Element("{%s}ListBucketResult" % XML_NAMESPACE)
        volume = await self.get_volume(request.user, bucket, 'list')

        requested_prefix = request.GET.get('prefix', '')
        max_keys = int(request.GET.get('max-keys', 100))
        encoding_type = request.GET.get('encoding-type', 'url')
        delimiter = request.GET.get('delimiter', '/')

        base_lookup = Blob.objects.filter(
            prefix=make_absolute_prefix(requested_prefix),
            volume=volume,
        ).order_by('path').select_related('volume__storage')

        blobs_qs = base_lookup.exclude(attributes__has_key=ATTR_BLOB_IS_FOLDER)
        folders_qs = base_lookup.filter(attributes__has_key=ATTR_BLOB_IS_FOLDER)

        blobs = []
        async for blob in blobs_qs[:max_keys].aiterator():
            blobs.append(blob)

        total_count = await blobs_qs.acount()
        is_truncated = max_keys < total_count

        ElementTree.SubElement(root, "Name").text = volume.name
        ElementTree.SubElement(root, "Prefix").text = requested_prefix
        ElementTree.SubElement(root, "KeyCount").text = str(len(blobs))
        ElementTree.SubElement(root, "MaxKeys").text = str(max_keys)
        ElementTree.SubElement(root, "Delimiter").text = delimiter
        ElementTree.SubElement(root, "EncodingType").text = encoding_type
        ElementTree.SubElement(root, "IsTruncated").text = str(is_truncated).lower()

        if blobs:
            for blob in blobs:
                root.append(self._etree_for_blob(blob))
        elif requested_prefix != '':
            directory_blob = await self.get_blob(volume, make_absolute_prefix(requested_prefix))
            root.append(self._etree_for_blob(directory_blob))

        common_prefixes = ElementTree.Element("CommonPrefixes")
        async for blob in folders_qs.aiterator():
            ElementTree.SubElement(common_prefixes, 'Prefix').text = blob.filename
        if len(common_prefixes):
            root.append(common_prefixes)

        return XMLResponse(root)

    async def put(self, request, bucket):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTBucketPUT.html"""
        storage = await Storage.objects.filter(**{
            'tags__%s' % TAG_S3_DEFAULT_STORAGE: True
        }).afirst()
        if storage is None:
            LOGGER.warning("No Storage marked as default. Add the Tag '%s: true' to a storage instance.",
                           TAG_S3_DEFAULT_STORAGE)
            raise AWSAccessDenied
        if not await request.user.ahas_perm('p2_core.add_volume'):
            raise AWSAccessDenied
        volume, _ = await Volume.objects.aget_or_create(
            name=bucket, defaults={'storage': storage}
        )
        await VolumeACL.objects.aget_or_create(
            volume=volume, user=request.user,
            defaults={'permissions': ['read', 'write', 'delete', 'list', 'admin']},
        )
        return HttpResponse(status=200)

    async def delete(self, request, bucket):
        """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTBucketDELETE.html"""
        volume = await self.get_volume(request.user, bucket, 'delete')
        await volume.adelete()
        return HttpResponse(status=204)
