#!/usr/bin/env bash
# One-time setup script for p2 on a new system.
# Run from the project root: bash scripts/setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }
die()   { echo -e "\033[0;31mERROR:${NC} $*" >&2; exit 1; }

# ── Environment Setup ─────────────────────────────────────────────────────────
cd "$REPO_ROOT"

if [[ ! -f "$REPO_ROOT/.env" ]]; then
    if [[ -f "$REPO_ROOT/.env.example" ]]; then
        info "No .env file found. Creating one automatically from .env.example..."
        cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    else
        die "No .env file found, and .env.example is missing!"
    fi
fi

_get_env_value() {
    local key="$1"
    local file="${2:-$REPO_ROOT/.env}"
    local val=""
    if [[ -f "$file" ]]; then
        val="$(grep -E "^${key}=" "$file" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
    fi
    echo "$val"
}

_set_env_value() {
    local key="$1"
    local value="$2"
    local env_file="$REPO_ROOT/.env"
    local escaped

    escaped="$(printf '%s' "$value" | sed 's/[\/&]/\\&/g')"
    if grep -q -E "^${key}=" "$env_file"; then
        sed -i "s/^${key}=.*/${key}=${escaped}/" "$env_file"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$env_file"
    fi
}

_generate_secret_key() {
    if command -v python3 &>/dev/null; then
        python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
    elif command -v openssl &>/dev/null; then
        openssl rand -base64 64 | tr -d '\n' | tr '+/' '-_' | tr -d '='
    else
        die "Cannot generate P2_SECRET_KEY automatically: install python3 or openssl."
    fi
}

_generate_fernet_key() {
    if command -v python3 &>/dev/null; then
        python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
    elif command -v openssl &>/dev/null; then
        openssl rand -base64 32 | tr -d '\n' | tr '+/' '-_'
    else
        die "Cannot generate P2_FERNET_KEY automatically: install python3 or openssl."
    fi
}

_ensure_generated_secret() {
    local key="$1"
    local current="$(_get_env_value "$key")"

    if [[ -n "$current" && "$current" != change-me-* && "$current" != CHANGE-ME-* ]]; then
        return
    fi

    if [[ "$key" == "P2_SECRET_KEY" ]]; then
        info "Generating $key..."
        _set_env_value "$key" "$(_generate_secret_key)"
    elif [[ "$key" == "P2_FERNET_KEY" ]]; then
        info "Generating $key..."
        _set_env_value "$key" "$(_generate_fernet_key)"
    fi
}

# ── Resolve host storage root ────────────────────────────────────────────────
# docker-compose bind-mounts this host path to /storage in every service (see
# P2_HOST_STORAGE_ROOT in docker-compose.yml); containers always use /storage
# internally.
_resolve_host_storage_root() {
    local val=""

    val="$(_get_env_value P2_HOST_STORAGE_ROOT)"
    if [[ -z "$val" ]]; then
        val="$(_get_env_value P2_HOST_STORAGE_ROOT "$REPO_ROOT/.env.example")"
    fi

    if [[ -z "$val" ]]; then
        val="$(_get_env_value P2_STORAGE__ROOT)"
    fi
    if [[ -z "$val" ]]; then
        val="$(_get_env_value P2_STORAGE__ROOT "$REPO_ROOT/.env.example")"
    fi

    if [[ -z "$val" || "$val" == "/storage" ]]; then
        val="$REPO_ROOT/storage"
    fi
    if [[ "$val" != /* ]]; then
        val="$REPO_ROOT/$val"
    fi
    echo "$val"
}

HOST_STORAGE_ROOT="$(_resolve_host_storage_root)"
CONTAINER_STORAGE_ROOT="/storage"
STATIC_ROOT="$REPO_ROOT/static"

info "Host storage root      : $HOST_STORAGE_ROOT"
info "Container storage root : $CONTAINER_STORAGE_ROOT"
info "Static root  : $STATIC_ROOT"

# ── Directories ────────────────────────────────────────────────────────────────
info "Creating required directories..."
mkdir -p "$HOST_STORAGE_ROOT/volumes"
mkdir -p "$STATIC_ROOT"
chmod 755 "$HOST_STORAGE_ROOT"
chmod 755 "$STATIC_ROOT"

info "Updating .env for Docker..."
_ensure_generated_secret "P2_SECRET_KEY"
_ensure_generated_secret "P2_FERNET_KEY"
_set_env_value "P2_HOST_STORAGE_ROOT" "$HOST_STORAGE_ROOT"
_set_env_value "P2_STORAGE__ROOT" "$CONTAINER_STORAGE_ROOT"
_set_env_value "P2_REDIS__HOST" "redis"
_set_env_value "P2_REDIS__ARQ_URL" "redis://redis:6379/1"
_set_env_value "P2_SECURITY__SSL_REDIRECT" "false"

# ── Reverse proxy ──────────────────────────────────────────────────────────────
# p2 no longer needs one. Granian serves the S3 data plane, the Django control
# plane and the SPA directly on port 8787.
#
# This step used to install an nginx site so object reads could be handed off via
# X-Accel-Redirect. That made sense when each object was its own file on disk.
# With the append-only volume layout an object's bytes are one or more block
# ranges inside a shared volume file, so there is no single file for sendfile()
# to hand out and the redirect has nothing to point at.

# ── Docker ─────────────────────────────────────────────────────────────────────
info "Building and starting Docker services..."
docker compose up --build -d

info "Recalculating volume stats from metadata..."
docker compose exec -T web python manage.py recalculate_space_used

info "Done. p2 should be available at http://localhost:8787"
info "Default login: admin / admin"
