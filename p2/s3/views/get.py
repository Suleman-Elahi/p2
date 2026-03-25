"""p2 S3 views"""
from xml.etree import ElementTree

from django.views import View

from p2.core.models import Volume
from p2.s3.constants import XML_NAMESPACE
from p2.s3.http import XMLResponse


class ListView(View):
    """https://docs.aws.amazon.com/AmazonS3/latest/API/RESTServiceGET.html"""

    def get(self, request):
        """Return list of Buckets the authenticated user can access."""
        root = ElementTree.Element("{%s}ListAllMyBucketsResult" % XML_NAMESPACE)
        owner = ElementTree.Element("Owner")

        ElementTree.SubElement(owner, 'ID').text = str(request.user.id)
        ElementTree.SubElement(owner, 'DisplayName').text = request.user.username

        buckets = ElementTree.Element('Buckets')

        # Return volumes the user has an ACL entry for (or public_read volumes)
        from p2.core.acl import VolumeACL
        from django.db.models import Q
        accessible_volumes = Volume.objects.filter(
            Q(public_read=True) |
            Q(acls__user=request.user) |
            Q(acls__group__in=request.user.groups.all())
        ).distinct()

        for volume in accessible_volumes:
            bucket = ElementTree.Element("Bucket")
            ElementTree.SubElement(bucket, "Name").text = volume.name
            ElementTree.SubElement(bucket, "CreationDate").text = "2006-02-03T16:45:09.000Z"
            buckets.append(bucket)

        root.append(owner)
        root.append(buckets)

        return XMLResponse(root)
