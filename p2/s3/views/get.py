"""p2 S3 Service-level views"""
from xml.etree import ElementTree

from django.db.models import Q
from django.views import View

from p2.core.acl import VolumeACL
from p2.core.models import Volume
from p2.s3.constants import XML_NAMESPACE
from p2.s3.http import XMLResponse
from p2.s3.views.common import S3View


class ListView(S3View):
    """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTServiceGET.html"""

    async def get(self, request):
        """Return list of buckets the authenticated user can access."""
        root = ElementTree.Element("{%s}ListAllMyBucketsResult" % XML_NAMESPACE)
        actual_user = await self._get_actual_user(request.user)

        # Handle anonymous users
        if not actual_user.is_authenticated:
            owner = ElementTree.SubElement(root, "Owner")
            ElementTree.SubElement(owner, 'ID').text = "anonymous"
            ElementTree.SubElement(owner, 'DisplayName').text = "Anonymous"
            buckets = ElementTree.SubElement(root, 'Buckets')
            
            # Only show public volumes for anonymous users
            async for volume in Volume.objects.filter(public_read=True).aiterator():
                bucket = ElementTree.SubElement(buckets, "Bucket")
                ElementTree.SubElement(bucket, "Name").text = volume.name
                ElementTree.SubElement(bucket, "CreationDate").text = "2006-02-03T16:45:09.000Z"
            
            return XMLResponse(root)

        owner = ElementTree.SubElement(root, "Owner")
        ElementTree.SubElement(owner, 'ID').text = str(actual_user.id)
        ElementTree.SubElement(owner, 'DisplayName').text = actual_user.username

        buckets = ElementTree.SubElement(root, 'Buckets')

        group_ids = [
            gid async for gid in
            actual_user.groups.values_list('pk', flat=True).aiterator()
        ]

        async for volume in Volume.objects.filter(
            Q(public_read=True) |
            Q(acls__user=actual_user) |
            Q(acls__group__in=group_ids)
        ).distinct().aiterator():
            bucket = ElementTree.SubElement(buckets, "Bucket")
            ElementTree.SubElement(bucket, "Name").text = volume.name
            ElementTree.SubElement(bucket, "CreationDate").text = "2006-02-03T16:45:09.000Z"

        return XMLResponse(root)
