"""p2 s3 routing middleware"""
import asyncio
import logging

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.utils.decorators import sync_and_async_middleware

from p2.lib.config import CONFIG
from p2.s3.auth.aws_v4 import AWSV4Authentication
from p2.s3.errors import AWSError, AWSMissingContentLength
from p2.s3.http import AWSErrorView

LOGGER = logging.getLogger(__name__)
CONTENT_LENGTH_HEADER = 'CONTENT_LENGTH'


def _s3_error(exception):
    if isinstance(exception, AWSError):
        if CONFIG.y_bool('debug'):
            LOGGER.debug("S3 Error: %s", exception)
        return AWSErrorView(exception)
    from django.http import Http404
    if isinstance(exception, Http404):
        return None
    LOGGER.exception("S3 Error: %s", exception)
    return None


def _extract_bucket(request):
    s3_base = '.' + CONFIG.y('s3.base_domain')
    host = request.META.get('HTTP_HOST', '').split(':')[0]
    if host.endswith(s3_base):
        return host.replace(s3_base, '')
    return False


def _is_s3(request):
    # Ignore well-known browser/system probes
    if request.path.startswith('/.well-known/') or request.path == '/favicon.ico':
        return False
    if 'HTTP_X_AMZ_DATE' in request.META:
        return True
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth.startswith('AWS') or auth.startswith('Bearer'):
        return True
    if 'X-Amz-Signature' in request.GET or 'X-P2-Signature' in request.GET:
        return True
    return False


def _check_content_length(request):
    if request.method != 'PUT':
        return
    if CONTENT_LENGTH_HEADER not in request.META:
        raise AWSMissingContentLength
    val = request.META.get(CONTENT_LENGTH_HEADER)
    if val == '' or int(val) < 0:
        raise AWSError


def _prepare(request, bucket):
    request.urlconf = 'p2.s3.explicit_urls'
    if bucket:
        request.path = '/' + bucket + request.path
        request.path_info = '/' + bucket + request.path_info
    _check_content_length(request)
    setattr(request, '_dont_enforce_csrf_checks', True)
    request._s3_request = True
    request.user = AnonymousUser()


@sync_and_async_middleware
def S3RoutingMiddleware(get_response):
    if asyncio.iscoroutinefunction(get_response):
        async def middleware(request: HttpRequest):
            bucket = _extract_bucket(request)
            is_s3 = _is_s3(request) or bucket

            if not is_s3:
                return await get_response(request)

            try:
                _prepare(request, bucket)
                if not ('X-P2-Signature' in request.GET) and AWSV4Authentication.can_handle(request):
                    handler = AWSV4Authentication(request)
                    request.user = await handler.validate()
                    request._s3_authenticated_user = request.user
            except AWSError as exc:
                return _s3_error(exc)

            try:
                response = await get_response(request)
                if request.method == 'OPTIONS' and response.status_code == 405:
                    response.status_code = 200
                return response
            except AWSError as exc:
                return _s3_error(exc)
    else:
        def middleware(request: HttpRequest):
            bucket = _extract_bucket(request)
            is_s3 = _is_s3(request) or bucket

            if not is_s3:
                return get_response(request)

            try:
                _prepare(request, bucket)
                if not ('X-P2-Signature' in request.GET) and AWSV4Authentication.can_handle(request):
                    from asgiref.sync import async_to_sync
                    handler = AWSV4Authentication(request)
                    request.user = async_to_sync(handler.validate)()
                    request._s3_authenticated_user = request.user
            except AWSError as exc:
                return _s3_error(exc)

            try:
                return get_response(request)
            except AWSError as exc:
                return _s3_error(exc)

    return middleware
