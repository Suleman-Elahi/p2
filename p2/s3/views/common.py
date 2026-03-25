"""common s3 views"""
import base64
import logging
import time
from hashlib import md5

from django.views import View
from django.views.decorators.csrf import csrf_exempt

from p2.core.acl import has_volume_permission
from p2.core.models import Blob, Volume
from p2.core.telemetry import s3_latency_histogram, s3_request_counter, tracer
from p2.s3.errors import (AWSBadDigest, AWSError, AWSInvalidDigest,
                          AWSNoSuchBucket, AWSNoSuchKey)

CONTENT_MD5_HEADER = 'HTTP_CONTENT_MD5'
X_AMZ_ACL_HEADER = 'HTTP_X_AMZ_ACL'

LOGGER = logging.getLogger(__name__)

VALID_ACLS = [
    "private", "public-read", "public-read-write", "aws-exec-read",
    "authenticated-read", "bucket-owner-read", "bucket-owner-full-control",
]


class S3View(View):
    """Base View for all S3 Views. Checks for common Headers and does database lookups."""

    def _check_content_md5(self):
        """Validate Content-MD5 Header (length and validity)"""
        if CONTENT_MD5_HEADER in self.request.META:
            if self.request.META.get(CONTENT_MD5_HEADER) == '':
                raise AWSInvalidDigest
            if len(self.request.META.get(CONTENT_MD5_HEADER)) < 24:
                raise AWSInvalidDigest
            hasher = md5()
            hasher.update(self.request.body)
            ours = base64.b64encode(hasher.digest()).decode('utf-8')
            if self.request.META.get(CONTENT_MD5_HEADER) != ours:
                LOGGER.debug("Got bad digest: theirs=%s ours=%s",
                             self.request.META.get(CONTENT_MD5_HEADER), ours)
                raise AWSBadDigest

    def apply_acl_permissions(self):
        """Parse x-amz-acl Header into p2 permissions, returned as List"""
        header = self.request.META.get(X_AMZ_ACL_HEADER)
        if not header:
            return
        if header not in VALID_ACLS:
            raise AWSError

    async def get_volume(self, user, bucket_name: str, permission: str) -> Volume:
        """Look up a Volume by name and verify the user has the given permission.

        Uses VolumeACL / has_volume_permission() instead of django-guardian.
        Raises AWSNoSuchBucket if the volume does not exist or access is denied.
        """
        try:
            volume = await Volume.objects.aget(name=bucket_name)
        except Volume.DoesNotExist:
            raise AWSNoSuchBucket
        allowed = await has_volume_permission(user, volume, permission)
        if not allowed:
            raise AWSNoSuchBucket
        return volume

    async def get_blob(self, volume: Volume, path: str) -> Blob:
        """Look up a Blob by volume and path. Raises AWSNoSuchKey if not found."""
        blob = await Blob.objects.filter(volume=volume, path=path).afirst()
        if blob is None:
            raise AWSNoSuchKey
        return blob

    async def dispatch(self, request, *args, **kwargs):
        """Wrap every S3 request in an OTel span and record counter/latency metrics.

        Satisfies Requirements 9.3, 9.8.
        """
        bucket = kwargs.get("bucket", "")
        key = kwargs.get("path", "")
        method = request.method.upper()

        with tracer.start_as_current_span("s3.request") as span:
            span.set_attribute("http.method", method)
            span.set_attribute("s3.bucket", bucket)
            span.set_attribute("s3.key", key)

            start = time.monotonic()
            response = await super().dispatch(request, *args, **kwargs)
            latency_ms = (time.monotonic() - start) * 1000

            status_code = response.status_code
            span.set_attribute("http.status_code", status_code)
            span.set_attribute("s3.latency_ms", latency_ms)

            attrs = {"method": method, "bucket": bucket}
            s3_request_counter.add(1, attrs)
            s3_latency_histogram.record(latency_ms, attrs)

            return response

    @csrf_exempt
    def setup(self, *args, **kwargs):
        super().setup(*args, **kwargs)
        self._check_content_md5()
        self.apply_acl_permissions()
