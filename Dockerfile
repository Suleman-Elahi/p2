# syntax=docker/dockerfile:1

FROM node:20-slim AS ui-builder
WORKDIR /app
COPY ui/package*.json ./ui/
RUN cd ui && npm ci --silent
COPY ui/ ./ui/
RUN cd ui && npm run build

FROM python:3.12.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./

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
COPY pyproject.toml uv.lock manage.py ./
COPY entrypoint.sh /entrypoint.sh
COPY p2/ ./p2/
COPY --from=ui-builder /app/ui/dist ./ui/dist

ENV PATH="/app/.venv/bin:$PATH"
ENV DJANGO_SETTINGS_MODULE=p2.core.settings

RUN chmod +x /entrypoint.sh \
    && useradd --create-home --shell /bin/false p2 \
    && mkdir -p /storage /app/static \
    && chown -R p2:p2 /storage /app/static \
    && chown p2:p2 /app /entrypoint.sh \
    && chown -R p2:p2 /app/p2 /app/manage.py /app/pyproject.toml /app/ui

USER p2

EXPOSE 8787
ENTRYPOINT ["/entrypoint.sh"]
