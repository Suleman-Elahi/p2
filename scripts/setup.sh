#!/usr/bin/env bash
# One-time setup script for p2 on a new system.
# Run from the project root: bash scripts/setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }
die()   { echo -e "\033[0;31mERROR:${NC} $*" >&2; exit 1; }

# ── Resolve storage root ────────────────────────────────────────────────────────
# Read P2_STORAGE__ROOT from .env if present, otherwise fall back to ./storage.
# The value may be absolute (/mnt/data/p2) or relative to the repo (./storage).
_resolve_storage_root() {
    local env_file="$REPO_ROOT/.env"
    local val=""
    if [[ -f "$env_file" ]]; then
        val="$(grep -E '^P2_STORAGE__ROOT=' "$env_file" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
    fi
    # Fall back to .env.example value
    if [[ -z "$val" && -f "$REPO_ROOT/.env.example" ]]; then
        val="$(grep -E '^P2_STORAGE__ROOT=' "$REPO_ROOT/.env.example" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
    fi
    # Still empty → default
    if [[ -z "$val" || "$val" == "/storage" ]]; then
        val="$REPO_ROOT/storage"
    fi
    # Make absolute (relative paths are relative to repo root)
    if [[ "$val" != /* ]]; then
        val="$REPO_ROOT/$val"
    fi
    echo "$val"
}

STORAGE_ROOT="$(_resolve_storage_root)"
STATIC_ROOT="$REPO_ROOT/static"

info "Storage root : $STORAGE_ROOT"
info "Static root  : $STATIC_ROOT"

# ── Directories ────────────────────────────────────────────────────────────────
info "Creating required directories..."
mkdir -p "$STORAGE_ROOT/volumes"
mkdir -p "$STATIC_ROOT"
chmod 755 "$STORAGE_ROOT"
chmod 755 "$STATIC_ROOT"

# ── Nginx config ───────────────────────────────────────────────────────────────
_install_nginx() {
    local template="$REPO_ROOT/deploy/nginx-host.conf"
    [[ -f "$template" ]] || die "nginx template not found: $template"

    local dest_available="/etc/nginx/sites-available/p2"
    local dest_enabled="/etc/nginx/sites-enabled/p2"
    # Also write the root-level convenience copy (gitignored)
    local dev_copy="$REPO_ROOT/nginx-p2.conf"

    # Substitute both __STORAGE_PATH__ and __STATIC_PATH__ from the template.
    local rendered
    rendered="$(sed \
        -e "s|__STORAGE_PATH__|${STORAGE_ROOT}|g" \
        -e "s|__STATIC_PATH__|${STATIC_ROOT}|g" \
        "$template")"

    # Write the system-installed config (requires sudo)
    sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
    echo "$rendered" | sudo tee "$dest_available" > /dev/null
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo ln -sf "$dest_available" "$dest_enabled"

    # Write the local dev copy (no sudo needed, gitignored)
    echo "$rendered" > "$dev_copy"
    info "Written local nginx config: $dev_copy"

    # Patch nginx.conf include on Arch Linux / minimal installs
    if ! grep -q "sites-enabled" /etc/nginx/nginx.conf; then
        info "Patching /etc/nginx/nginx.conf to include sites-enabled..."
        sudo sed -i '/http {/a \    include /etc/nginx/sites-enabled/*;\n    types_hash_max_size 2048;\n    types_hash_bucket_size 64;' /etc/nginx/nginx.conf
    fi

    sudo nginx -t || die "nginx config test failed — fix errors above then re-run."

    if command -v systemctl &>/dev/null; then
        if systemctl is-active --quiet nginx; then
            sudo systemctl reload nginx
        else
            sudo systemctl enable --now nginx
        fi
    else
        sudo nginx -s reload 2>/dev/null || sudo nginx
    fi

    info "Nginx configured and reloaded."
}

if command -v nginx &>/dev/null; then
    info "Installing nginx config..."
    _install_nginx
else
    warn "nginx not found — skipping nginx setup (X-Accel-Redirect will be unavailable)."
    warn "Set P2_STORAGE__USE_X_ACCEL_REDIRECT=false in .env to use pure-Python file serving."
fi

# ── Docker ─────────────────────────────────────────────────────────────────────
info "Building and starting Docker services..."
cd "$REPO_ROOT"
docker compose up --build -d

info "Done. p2 should be available at http://localhost"
info "Default login: admin / admin"
