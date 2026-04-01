# syntax=docker/dockerfile:1

FROM python:3.12.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml ./

# Cache uv's package downloads across builds
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

FROM python:3.12.13-slim AS app

WORKDIR /app

# Cache apt packages across builds
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends libmagic1

COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml manage.py ./
COPY entrypoint.sh /entrypoint.sh
COPY p2/ ./p2/

ENV PATH="/app/.venv/bin:$PATH"
ENV DJANGO_SETTINGS_MODULE=p2.core.settings

# Bake static files into the image at build time
RUN python manage.py collectstatic --noinput

RUN chmod +x /entrypoint.sh \
    && useradd --create-home --shell /bin/false p2 \
    && mkdir -p /storage \
    && chown -R p2:p2 /storage /app/static \
    && chown p2:p2 /app /entrypoint.sh \
    && chown -R p2:p2 /app/p2 /app/manage.py /app/pyproject.toml

USER p2

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
