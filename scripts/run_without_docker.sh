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

# ── Environment Setup ─────────────────────────────────────────────────────────
if [[ ! -f "$REPO_ROOT/.env" ]]; then
    if [[ -f "$REPO_ROOT/.env.example" ]]; then
        info "No .env file found. Creating one automatically from .env.example..."
        cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    else
        die "No .env file found, and .env.example is missing!"
    fi
fi

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

# ── Port ───────────────────────────────────────────────────────────────────────
PORT=8787

# ── Nginx config (regenerate from template so paths are always correct) ─────────
if ! command -v nginx &>/dev/null; then
    warn "nginx not found. Installing..."
    sudo apt-get update && sudo apt-get install -y nginx
fi

if command -v nginx &>/dev/null; then
    DEV_CONF="$REPO_ROOT/nginx-p2.conf"
    info "Generating nginx-p2.conf inline..."
    cat > "$DEV_CONF" <<EOF
upstream granian {
    server 127.0.0.1:${PORT};
    keepalive 128;
}

server {
    listen 80;
    server_name localhost _;

    client_max_body_size 2G;
    access_log off;

    location /static/ {
        alias ${STATIC_ROOT}/;
        expires 7d;
    }

    location /internal-storage/ {
        internal;
        alias ${STORAGE_ROOT}/;
        sendfile on;
        tcp_nopush on;
        aio threads;
    }

    location / {
        proxy_pass http://granian;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_redirect off;

        # Stream request body directly to granian without buffering to disk.
        proxy_request_buffering off;
        proxy_buffering off;
    }
}
EOF
    info "nginx-p2.conf written (storage=\$STORAGE_ROOT, port=\$PORT)"

    info "Replacing nginx config and reloading (may require sudo password)..."
    sudo cp "$DEV_CONF" "/etc/nginx/sites-available/p2.conf"
    sudo ln -sf "/etc/nginx/sites-available/p2.conf" "/etc/nginx/sites-enabled/p2.conf"
    # Remove stale legacy symlinks that may point to old configs (e.g. port 8000)
    sudo rm -f "/etc/nginx/sites-enabled/default" "/etc/nginx/sites-enabled/p2"
    sudo systemctl reload nginx
else
    warn "nginx not found — X-Accel-Redirect will not work."
    warn "Set P2_STORAGE__USE_X_ACCEL_REDIRECT=false in .env to use pure-Python serving."
fi

# ── Dependencies ───────────────────────────────────────────────────────────────
info "Syncing dependencies..."
uv sync --python 3.12

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

CORES=$(nproc)
WORKERS=$((CORES * 1))
info "Starting granian (${WORKERS} workers based on ${CORES} CPU cores)..."
IS_DEBUG=false
if grep -iq '^P2_DEBUG=true' .env 2>/dev/null; then
    IS_DEBUG=true
fi

GRANIAN_ARGS=(
    --interface asginl
    --workers "$WORKERS"
    --loop uvloop
    --host 0.0.0.0
    --port "$PORT"
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

info "p2 running at http://localhost:$PORT (granian) — Ctrl+C to stop"
wait
