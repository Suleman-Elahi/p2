"""p2 S3 Service-level views"""
from xml.etree import ElementTree

from django.db.models import Q
from django.views import View

from p2.core.acl import VolumeACL
from p2.core.models import Volume
from p2.s3.constants import XML_NAMESPACE
from p2.s3.http import XMLResponse


class ListView(View):
    """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTServiceGET.html"""

    async def get(self, request):
        """Return list of buckets the authenticated user can access."""
        root = ElementTree.Element("{%s}ListAllMyBucketsResult" % XML_NAMESPACE)

        owner = ElementTree.SubElement(root, "Owner")
        ElementTree.SubElement(owner, 'ID').text = str(request.user.id)
        ElementTree.SubElement(owner, 'DisplayName').text = request.user.username

        buckets = ElementTree.SubElement(root, 'Buckets')

        group_ids = [
            gid async for gid in
            request.user.groups.values_list('pk', flat=True).aiterator()
        ]

        async for volume in Volume.objects.filter(
            Q(public_read=True) |
            Q(acls__user=request.user) |
            Q(acls__group__in=group_ids)
        ).distinct().aiterator():
            bucket = ElementTree.SubElement(buckets, "Bucket")
            ElementTree.SubElement(bucket, "Name").text = volume.name
            ElementTree.SubElement(bucket, "CreationDate").text = "2006-02-03T16:45:09.000Z"

        return XMLResponse(root)
