"""
ASGI config for p2 project.

Uses uvicorn with uvloop as the event loop for async-first operation.
OpenTelemetry is initialised before Django's ASGI app so DjangoInstrumentor
can wrap the middleware stack at import time.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")

# Expand the default threadpool so asyncio.to_thread() calls (LMDB reads,
# file writes) don't queue behind each other under concurrent load.
# Each uvicorn worker process gets its own pool — this sets it per-worker.
import asyncio
_loop = asyncio.new_event_loop()
_loop.set_default_executor(ThreadPoolExecutor(max_workers=32))
asyncio.set_event_loop(_loop)

from django.core.asgi import get_asgi_application  # noqa: E402

if os.environ.get("OTEL_SDK_DISABLED", "false").lower() != "true":
    from p2.core.telemetry import setup_telemetry  # noqa: E402
    setup_telemetry()

application = get_asgi_application()

try:
    from p2.s3.asgi_handler import S3ProxyASGIApp
    application = S3ProxyASGIApp(application)
except ImportError as e:
    import logging
    logging.getLogger(__name__).warning("Failed to load S3 ASGI protocol fastpath, defaulting to Django: %s", e)
