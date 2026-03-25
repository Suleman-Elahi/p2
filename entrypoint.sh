#!/bin/bash
set -e
uv run python manage.py migrate --noinput
exec uvicorn p2.core.asgi:application --host ${HOST:-0.0.0.0} --port ${PORT:-8000}
