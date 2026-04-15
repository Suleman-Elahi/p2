"""
RSGI entry point for p2.

Granian's native RSGI protocol — lower overhead than ASGI because the
scope/proto objects are Rust-backed and avoid the ASGI receive/send
coroutine dispatch overhead for the common HTTP case.

Architecture:
  - S3 data-plane (GET/PUT single objects) → handled natively in RSGI
    using proto.response_file() for zero-copy GET and direct body
    iteration for PUT.
  - Everything else → bridged to the existing ASGI application via
    the ASGI proto shim Granian exposes on the proto object
    (proto.receive / proto.send).
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")

# Expand threadpool before any Django import so all workers inherit it.
import asyncio
_loop = asyncio.new_event_loop()
_loop.set_default_executor(ThreadPoolExecutor(max_workers=32))
asyncio.set_event_loop(_loop)

if os.environ.get("OTEL_SDK_DISABLED", "false").lower() != "true":
    from p2.core.telemetry import setup_telemetry
    setup_telemetry()

from django.core.asgi import get_asgi_application  # noqa: E402
_asgi_app = get_asgi_application()

# Build the ASGI scope dict from an RSGI scope for the Django fallback.
def _asgi_scope(scope):
    headers = [
        (k.encode('latin1'), v.encode('latin1'))
        for k, v in scope.headers.items()
    ]
    return {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': scope.http_version,
        'method': scope.method,
        'headers': headers,
        'path': scope.path,
        'query_string': scope.query_string.encode('ascii'),
        'root_path': '',
        'scheme': scope.scheme,
        'server': tuple(scope.server.rsplit(':', 1)) if scope.server else ('localhost', 80),
        'client': tuple(scope.client.rsplit(':', 1)) if scope.client else None,
    }


async def _django_fallback(scope, proto):
    """Bridge RSGI proto → ASGI receive/send and call Django."""
    await _asgi_app(_asgi_scope(scope), proto.receive, proto.send)


# ── S3 RSGI fastpath ──────────────────────────────────────────────────────────

try:
    from p2.s3.rsgi_handler import S3ProxyRSGIApp
    application = S3ProxyRSGIApp(_django_fallback)
except ImportError as e:
    import logging
    logging.getLogger(__name__).warning(
        "Failed to load S3 RSGI fastpath, falling back to ASGI Django: %s", e
    )
    application = _django_fallback
