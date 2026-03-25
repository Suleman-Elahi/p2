# syntax=docker/dockerfile:1

# ── Builder stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml ./

RUN uv sync --no-install-project --no-dev

# ── Final stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS final

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

COPY pyproject.toml ./
COPY manage.py ./
COPY p2/ ./p2/

RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd --create-home --shell /bin/false p2 \
    && chown -R p2:p2 /app
USER p2

ENV HOST=0.0.0.0
ENV PORT=8000
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE ${PORT}

ENTRYPOINT ["/entrypoint.sh"]
