"""
ASGI config for p2 project.

Uses uvicorn with uvloop as the event loop for async-first operation.
OpenTelemetry is initialised before Django's ASGI app so DjangoInstrumentor
can wrap the middleware stack at import time.
"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

if os.environ.get("OTEL_SDK_DISABLED", "false").lower() != "true":
    from p2.core.telemetry import setup_telemetry  # noqa: E402
    setup_telemetry()

application = get_asgi_application()
