"""p2 s3 routing middleware"""
from django.contrib.auth.signals import user_logged_in
from django.http import HttpRequest
import logging

from p2.lib.config import CONFIG
from p2.s3.auth.aws_v4 import AWSV4Authentication
from p2.s3.errors import AWSError, AWSIncompleteBody, AWSMissingContentLength
from p2.s3.http import AWSErrorView

LOGGER = logging.getLogger(__name__)
CONTENT_LENGTH_HEADER = 'CONTENT_LENGTH'

# pylint: disable=too-few-public-methods
class S3RoutingMiddleware:
    """Handle request as S3 request if X-Amz-Date Header is set.

    Supports both sync and async modes so that non-S3 requests flow through
    the normal sync middleware chain (preserving session cookie handling)."""

    async_capable = True
    sync_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self._s3_base = '.' + CONFIG.y('s3.base_domain')
        import asyncio
        if asyncio.iscoroutinefunction(self.get_response):
            self._is_async = True
        else:
            self._is_async = False

    def process_exception(self, request: HttpRequest, exception):
        """Catch AWS-specific exceptions and show them as XML response"""
        if isinstance(exception, AWSError):
            if CONFIG.y_bool('debug'):
                LOGGER.debug("S3 Error: %s", exception)
            return AWSErrorView(exception)
        LOGGER.exception("S3 Error: %s", exception)
        return None

    def extract_host_header(self, request: HttpRequest):
        """Extract bucket name from Host Header"""
        host_header = request.META.get('HTTP_HOST', '')
        # Make sure we remove the port suffix, if any
        if ':' in host_header:
            host_header, _ = host_header.split(':')
        if host_header.endswith(self._s3_base):
            bucket = host_header.replace(self._s3_base, '')
            return bucket
        return False

    def is_aws_request(self, request: HttpRequest):
        """Return true if AWS-s3-style request"""
        if 'HTTP_X_AMZ_DATE' in request.META:
            return True
        if 'HTTP_AUTHORIZATION' in request.META:
            auth = request.META['HTTP_AUTHORIZATION']
            if auth.startswith('AWS') or auth.startswith('Bearer'):
                return True
        if 'X-Amz-Signature' in request.GET:
            return True
        return False

    def check_content_length(self, request):
        """Validate Content-Length Header (is required for PUT requests)"""
        if request.method != 'PUT':
            return
        if CONTENT_LENGTH_HEADER not in request.META and request.method == 'PUT':
            raise AWSMissingContentLength
        theirs = request.META.get(CONTENT_LENGTH_HEADER)
        if theirs == '':
            raise AWSError
        theirs_int = int(theirs)
        ours = len(request.body)
        if theirs_int < 0:
            raise AWSError
        if ours < theirs_int:
            raise AWSIncompleteBody

    def _prepare_s3_request(self, request: HttpRequest, bucket):
        """Shared setup for S3 requests (sync and async paths)."""
        request.urlconf = 'p2.s3.explicit_urls'
        if bucket:
            request.path = '/' + bucket + request.path
            request.path_info = '/' + bucket + request.path_info
        self.check_content_length(request)
        setattr(request, '_dont_enforce_csrf_checks', True)
        if request.method in ['GET', 'HEAD']:
            request.META['HTTP_X_FORWARDED_PROTO'] = 'https'

    def __call__(self, request: HttpRequest):
        if self._is_async:
            return self.__acall__(request)
        bucket = self.extract_host_header(request)
        if self.is_aws_request(request) or bucket:
            try:
                self._prepare_s3_request(request, bucket)
                # Sync path: AWS auth requires async validate(), run via async_to_sync
                if AWSV4Authentication.can_handle(request):
                    from asgiref.sync import async_to_sync
                    handler = AWSV4Authentication(request)
                    try:
                        user = async_to_sync(handler.validate)()
                        request.user = user
                        user_logged_in.send(sender=self, request=request, user=user)
                    except AWSError as exc:
                        return self.process_exception(request, exc)
            except AWSError as exc:
                return self.process_exception(request, exc)
        return self.get_response(request)

    async def __acall__(self, request: HttpRequest):
        bucket = self.extract_host_header(request)
        if self.is_aws_request(request) or bucket:
            try:
                self._prepare_s3_request(request, bucket)
                if AWSV4Authentication.can_handle(request):
                    handler = AWSV4Authentication(request)
                    user = await handler.validate()
                    request.user = user
                    user_logged_in.send(sender=self, request=request, user=user)
            except AWSError as exc:
                return self.process_exception(request, exc)
            try:
                return await self.get_response(request)
            except AWSError as exc:
                return self.process_exception(request, exc)
        return await self.get_response(request)
