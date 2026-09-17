# syntax=docker/dockerfile:1

FROM node:20-slim AS ui-builder
WORKDIR /app
COPY ui/package*.json ./ui/
RUN cd ui && npm ci --silent
COPY ui/ ./ui/
RUN cd ui && npm run build

# ---------------------------------------------------------------------------
# Rust extensions.
#
# These used to be committed to the repo as p2/s3/*.so and picked up by
# `COPY p2/ ./p2/`, so the image silently inherited whatever binary happened to
# be checked in — which drifted out of sync with the Rust source. They are now
# built from source here and installed into the venv as wheels, so the binary in
# the image always matches p2/s3/*/src/. See p2/s3/_native.py.
# ---------------------------------------------------------------------------
FROM python:3.12.13-slim AS rust-builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends curl build-essential

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:/root/.local/bin:$PATH"
RUN uv tool install maturin

WORKDIR /build
COPY p2/s3/rust_ext/ ./rust_ext/
COPY p2/s3/checksum_ext/ ./checksum_ext/

# rust_ext/.cargo/config.toml pins `target-cpu=native`, which would bake the
# build machine's instruction set into the wheel and fault on an older CPU at
# runtime. RUSTFLAGS takes precedence over config.toml's [build] rustflags, so
# this pins a portable baseline instead. Raise it if you control the hardware.
ENV RUSTFLAGS="-C target-cpu=x86-64-v2"

RUN --mount=type=cache,target=/root/.cargo/registry \
    cd rust_ext && maturin build --release --interpreter python3.12 --out /wheels
RUN --mount=type=cache,target=/root/.cargo/registry \
    cd checksum_ext && maturin build --release --interpreter python3.12 --out /wheels

FROM python:3.12.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./

# Cache uv's package downloads across builds
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

# Install the Rust extensions into the same venv the app stage copies out.
COPY --from=rust-builder /wheels /wheels
RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/app/.venv uv pip install --reinstall --no-deps /wheels/*.whl \
    && P2_REQUIRE_NATIVE=1 /app/.venv/bin/python -c \
       "import p2_s3_crypto, p2_s3_checksum; print('native extensions OK')"

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
