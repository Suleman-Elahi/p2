"""
ASGI config for p2 project.

Served by Granian in "asginl" (no-lifespan) mode, so there is no ASGI
lifespan.startup hook to hang per-worker setup off — Granian creates its own
event loop per worker process (see granian._loops) *before* this module is
imported, and drives that loop directly. Anything this module does with
asyncio.new_event_loop() / asyncio.set_event_loop() only changes "current
loop" bookkeeping; it has no effect on the loop Granian actually schedules
work on. Per-worker runtime setup (like sizing the thread pool executor used
by asyncio.to_thread()) must therefore be done lazily, on first request, using
asyncio.get_running_loop() from inside the app callable.

OpenTelemetry is initialised before Django's ASGI app so DjangoInstrumentor
can wrap the middleware stack at import time.
"""
import os
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

if os.environ.get("OTEL_SDK_DISABLED", "false").lower() != "true":
    from p2.core.telemetry import setup_telemetry  # noqa: E402
    setup_telemetry()

_django_app = get_asgi_application()

try:
    from p2.s3.asgi_handler import S3ProxyASGIApp
    _django_app = S3ProxyASGIApp(_django_app)
except ImportError as e:
    import logging
    logging.getLogger(__name__).warning("Failed to load S3 ASGI protocol fastpath, defaulting to Django: %s", e)

# Expand the default threadpool so asyncio.to_thread() calls (LMDB reads,
# file writes) don't queue behind each other under concurrent load. Sized
# lazily against the event loop Granian is actually running (one per worker
# process), the first time a request reaches this worker. No lock is needed:
# the ASGI app callable runs on a single-threaded event loop and there is no
# `await` between the check and the set, so two requests can't interleave here.
_EXECUTOR_MAX_WORKERS = int(os.environ.get("P2_ASGI_THREADPOOL_MAX_WORKERS", "32"))
_executor_configured = False


def _ensure_executor_configured() -> None:
    global _executor_configured
    if _executor_configured:
        return
    import asyncio
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_EXECUTOR_MAX_WORKERS)
    )
    _executor_configured = True


async def application(scope, receive, send):
    _ensure_executor_configured()
    await _django_app(scope, receive, send)
