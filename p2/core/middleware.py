"""p2 core middlewares"""
from django.http import HttpResponse
from django.utils.decorators import sync_and_async_middleware


@sync_and_async_middleware
def HealthCheckMiddleware(get_response):
    """Kubernetes health check middleware"""
    import asyncio
    if asyncio.iscoroutinefunction(get_response):
        async def middleware(request):
            if request.method == "GET" and \
                    request.META.get('HTTP_HOST', '') == 'kubernetes-healthcheck-host':
                return HttpResponse("OK")
            return await get_response(request)
    else:
        def middleware(request):
            if request.method == "GET" and \
                    request.META.get('HTTP_HOST', '') == 'kubernetes-healthcheck-host':
                return HttpResponse("OK")
            return get_response(request)
    return middleware


@sync_and_async_middleware
def S3AuthPreserveMiddleware(get_response):
    """Prevent Django's AuthenticationMiddleware from overwriting the S3-authenticated user."""
    import asyncio
    if asyncio.iscoroutinefunction(get_response):
        async def middleware(request):
            if getattr(request, '_s3_request', False):
                request._s3_user = getattr(request, 'user', None)
            response = await get_response(request)
            if getattr(request, '_s3_user', None) is not None:
                request.user = request._s3_user
            return response
    else:
        def middleware(request):
            if getattr(request, '_s3_request', False):
                request._s3_user = getattr(request, 'user', None)
            response = get_response(request)
            if getattr(request, '_s3_user', None) is not None:
                request.user = request._s3_user
            return response
    return middleware
