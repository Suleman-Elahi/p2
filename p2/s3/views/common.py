"""common s3 views"""
import base64
import logging
import time
from hashlib import md5

from django.views import View
from django.views.decorators.csrf import csrf_exempt

from p2.core.acl import has_volume_permission
from p2.core.models import Volume
from p2.core.telemetry import s3_latency_histogram, s3_request_counter
from p2.s3.errors import (AWSBadDigest, AWSError, AWSInvalidDigest,
                          AWSNoSuchBucket, AWSNoSuchKey)

CONTENT_MD5_HEADER = 'HTTP_CONTENT_MD5'
X_AMZ_ACL_HEADER = 'HTTP_X_AMZ_ACL'

LOGGER = logging.getLogger(__name__)

VALID_ACLS = [
    "private", "public-read", "public-read-write", "aws-exec-read",
    "authenticated-read", "bucket-owner-read", "bucket-owner-full-control",
]

# Map p2 permission names → S3 action for policy evaluation
_PERM_TO_S3_ACTION = {
    "read":   "s3:GetObject",
    "list":   "s3:ListBucket",
    "write":  "s3:PutObject",
    "delete": "s3:DeleteObject",
    "admin":  "s3:PutBucketPolicy",
}


async def _policy_allows(volume, permission: str, bucket_name: str, object_key: str) -> bool:
    """Return True if the bucket policy grants *permission* on *object_key* to everyone (Principal: *)."""
    import json
    from p2.s3.policy import check_access, parse_policy, AccessCheckResult
    policy_json = volume.tags.get('s3.p2.io/bucket-policy')
    if not policy_json:
        return False
    try:
        statements = parse_policy(policy_json)
    except Exception:
        return False

    action = _PERM_TO_S3_ACTION.get(permission)
    if not action:
        return False

    # Build the ARN for the object (or bucket for list)
    if object_key:
        key = object_key.lstrip('/')
        resource = f"arn:aws:s3:::{bucket_name}/{key}"
    else:
        resource = f"arn:aws:s3:::{bucket_name}"

    # Only honour statements with Principal: * (public) for anonymous access
    public_stmts = [
        s for s in statements
        if s.get('principal') == '*' or s.get('principal') == {'AWS': '*'}
    ]
    result = check_access(public_stmts, action, resource)
    return result == AccessCheckResult.ALLOW


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

    async def get_volume(self, user, bucket_name: str, permission: str,
                         object_key: str = '') -> Volume:
        """Look up a Volume by name and verify the user has the given permission."""
        from p2.s3.cache import get_cached_volume, set_cached_volume

        # Use the S3-authenticated user if available
        actual_user = getattr(self.request, '_s3_authenticated_user', user)

        # Try cache first — works for both public and private volumes
        cached = get_cached_volume(bucket_name)
        if cached:
            uuid_hex, public_read = cached
            import uuid as uuid_mod
            vol_uuid = uuid_mod.UUID(hex=uuid_hex)
            volume = Volume(name=bucket_name, public_read=public_read)
            volume.uuid = vol_uuid
            # pk must match uuid so ACL cache key (str(volume.pk)) is correct
            volume.pk = vol_uuid
            volume.tags = {}

            # Presigned token already validated upstream
            if getattr(self.request, '_presigned_validated', False):
                return volume

            # has_volume_permission has its own ACL cache — O(1) on warm path
            if await has_volume_permission(actual_user, volume, permission):
                return volume

            # Public volumes: also check bucket policy (tags={} so no policy)
            if public_read and permission in ("read", "list"):
                return volume

            LOGGER.warning("get_volume(cache): user '%s' denied '%s' on '%s'",
                           getattr(actual_user, 'username', '?'), permission, bucket_name)
            raise AWSNoSuchBucket

        # Cache miss — hit the DB once, then cache for all future requests
        try:
            volume = await Volume.objects.aget(name=bucket_name)
            set_cached_volume(bucket_name, volume.uuid.hex, volume.public_read)
        except Volume.DoesNotExist:
            LOGGER.warning("get_volume: Volume '%s' not found in database", bucket_name)
            raise AWSNoSuchBucket

        if getattr(self.request, '_presigned_validated', False):
            return volume

        allowed = await has_volume_permission(actual_user, volume, permission)
        if allowed:
            return volume

        if await _policy_allows(volume, permission, bucket_name, object_key):
            return volume

        LOGGER.warning("get_volume: user '%s' denied '%s' on '%s'",
                       getattr(actual_user, 'username', '?'), permission, bucket_name)
        raise AWSNoSuchBucket

    async def get_engine(self, volume: Volume):
        """Return the shared cached MetaEngine for this volume.
        
        The engine registry is thread-safe and the lookup is O(1) dict access,
        so we can call it directly without asyncio.to_thread overhead.
        """
        from p2.s3.engine import get_engine
        return get_engine(volume)

    async def get_blob(self, volume: Volume, path: str) -> dict:
        """Look up a Blob by volume and path. Raises AWSNoSuchKey if not found."""
        import json
        engine = await self.get_engine(volume)
        metadata_json = engine.get(path)
        if not metadata_json:
            raise AWSNoSuchKey
        return json.loads(metadata_json)

    async def dispatch(self, request, *args, **kwargs):
        """Wrap every S3 request, catch AWSErrors, and record metrics."""
        from p2.s3.errors import AWSError
        from p2.s3.http import AWSErrorView

        bucket = kwargs.get("bucket", "")
        method = request.method.upper()
        start = time.monotonic()

        try:
            response = await super().dispatch(request, *args, **kwargs)
        except AWSError as exc:
            response = AWSErrorView(exc)
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            attrs = {"method": method, "bucket": bucket}
            s3_request_counter.add(1, attrs)
            s3_latency_histogram.record(latency_ms, attrs)

        return response

    @csrf_exempt
    def setup(self, *args, **kwargs):
        super().setup(*args, **kwargs)
        self._check_content_md5()
        self.apply_acl_permissions()
