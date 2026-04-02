#!/usr/bin/env bash
# One-time setup script for p2 on a new system.
# Run from the project root: bash scripts/setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}WARN:${NC} $*"; }

# ── Directories ────────────────────────────────────────────────────────────────
info "Creating required directories..."
mkdir -p "$REPO_ROOT/storage"
mkdir -p "$REPO_ROOT/static"
chmod 777 "$REPO_ROOT/storage"
chmod 755 "$REPO_ROOT/static"

# ── Nginx config ───────────────────────────────────────────────────────────────
if command -v nginx &>/dev/null; then
    info "Installing nginx config..."
    NGINX_CONF="$REPO_ROOT/deploy/nginx-host.conf"
    NGINX_DEST="/etc/nginx/sites-available/p2"

    sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
    sudo cp "$NGINX_CONF" "$NGINX_DEST"
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo ln -sf "$NGINX_DEST" /etc/nginx/sites-enabled/p2

    # Patch nginx.conf for Arch Linux context if needed
    if ! grep -q "sites-enabled" /etc/nginx/nginx.conf; then
        info "Patching /etc/nginx/nginx.conf to include sites-enabled..."
        sudo sed -i '/http {/a \    include /etc/nginx/sites-enabled/*;\n    types_hash_max_size 2048;\n    types_hash_bucket_size 64;' /etc/nginx/nginx.conf
    fi

    sudo nginx -t
    if command -v systemctl &>/dev/null; then
        if systemctl is-active --quiet nginx; then
            sudo systemctl reload nginx
        else
            sudo systemctl enable --now nginx
        fi
    else
        sudo nginx -s reload || sudo nginx
    fi
    info "Nginx configured and started."
else
    warn "nginx not found — skipping nginx setup."
fi

# ── Docker ─────────────────────────────────────────────────────────────────────
info "Building and starting Docker services..."
cd "$REPO_ROOT"
docker compose up --build -d

info "Done. p2 should be available at http://localhost"
info "Default login: admin / admin"
