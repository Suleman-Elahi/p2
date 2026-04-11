#!/usr/bin/env bash
# Run p2 locally without Docker.
# Usage: bash scripts/run_without_docker.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}WARN:${NC} $*"; }
die()  { echo -e "\033[0;31mERROR:${NC} $*" >&2; exit 1; }

cd "$REPO_ROOT"

# ── Resolve storage root ────────────────────────────────────────────────────────
_resolve_storage_root() {
    local env_file="$REPO_ROOT/.env"
    local val=""
    if [[ -f "$env_file" ]]; then
        val="$(grep -E '^P2_STORAGE__ROOT=' "$env_file" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
    fi
    if [[ -z "$val" && -f "$REPO_ROOT/.env.example" ]]; then
        val="$(grep -E '^P2_STORAGE__ROOT=' "$REPO_ROOT/.env.example" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
    fi
    if [[ -z "$val" || "$val" == "/storage" ]]; then
        val="$REPO_ROOT/storage"
    fi
    if [[ "$val" != /* ]]; then
        val="$REPO_ROOT/$val"
    fi
    echo "$val"
}

STORAGE_ROOT="$(_resolve_storage_root)"
STATIC_ROOT="$REPO_ROOT/static"

# ── Dirs ───────────────────────────────────────────────────────────────────────
info "Creating required directories..."
mkdir -p "$STORAGE_ROOT/volumes" static

# ── Nginx config (regenerate from template so paths are always correct) ─────────
if command -v nginx &>/dev/null; then
    TEMPLATE="$REPO_ROOT/deploy/nginx-host.conf"
    DEV_CONF="$REPO_ROOT/nginx-p2.conf"
    if [[ -f "$TEMPLATE" ]]; then
        info "Regenerating nginx-p2.conf from template..."
        sed \
            -e "s|__STORAGE_PATH__|${STORAGE_ROOT}|g" \
            -e "s|__STATIC_PATH__|${STATIC_ROOT}|g" \
            "$TEMPLATE" > "$DEV_CONF"
        info "nginx-p2.conf written (storage=$STORAGE_ROOT)"
    fi
else
    warn "nginx not found — X-Accel-Redirect will not work."
    warn "Set P2_STORAGE__USE_X_ACCEL_REDIRECT=false in .env to use pure-Python serving."
fi

# ── Dependencies ───────────────────────────────────────────────────────────────
info "Syncing dependencies..."
uv sync

export P2_REDIS__HOST="127.0.0.1"
export REDIS_URL="redis://127.0.0.1:6379/0"
export ARQ_REDIS_URL="redis://127.0.0.1:6379/1"
export HOST="127.0.0.1"
export P2_STORAGE__ROOT="$STORAGE_ROOT"

# ── Django setup ───────────────────────────────────────────────────────────────
info "Running migrations..."
uv run python manage.py migrate --noinput

info "Collecting static files..."
uv run python manage.py collectstatic --noinput

# ── Launch ─────────────────────────────────────────────────────────────────────
# Match Docker entrypoint: raise fd limit for 8 workers under high concurrency
ulimit -n 65536 2>/dev/null || true
umask 022

info "Starting arq worker..."
uv run --env-file .env python -m arq p2.core.worker.WorkerSettings &
WORKER_PID=$!

info "Starting granian (4 workers)..."
IS_DEBUG=false
if grep -iq '^P2_DEBUG=true' .env 2>/dev/null; then
    IS_DEBUG=true
fi

GRANIAN_ARGS=(
    --interface asginl
    --workers 4
    --loop uvloop
    --host 0.0.0.0
    --port 8000
    --env-files .env
    --no-ws
    --working-dir "$REPO_ROOT"
)

if [[ "$IS_DEBUG" == "true" ]]; then
    GRANIAN_ARGS+=(--log --access-log --log-level info)
else
    GRANIAN_ARGS+=(--log --log-level error)
fi

uv run granian "${GRANIAN_ARGS[@]}" p2.core.asgi:application &
SERVER_PID=$!

# ── Cleanup on exit ────────────────────────────────────────────────────────────
trap "info 'Shutting down...'; kill $WORKER_PID $SERVER_PID 2>/dev/null; wait" SIGINT SIGTERM

info "p2 running at http://localhost:8000 — Ctrl+C to stop"
wait
