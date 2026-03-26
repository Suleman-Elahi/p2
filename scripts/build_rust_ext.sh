#!/usr/bin/env bash
# Build the p2_s3_crypto Rust extension and place it in p2/s3/.
#
# Automatically installs Rust (via rustup) and maturin if not present.
# Supported: Debian/Ubuntu, Arch Linux, macOS (Homebrew or standalone rustup).
#
# Run once before `docker compose up`, and again whenever p2/s3/rust_ext/ changes.
# The compiled .so is committed to the repo — Docker needs no Rust toolchain.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_DIR="$REPO_ROOT/p2/s3/rust_ext"
OUT_DIR="$EXT_DIR/dist"
DEST="$REPO_ROOT/p2/s3"

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}==>${NC} $*"; }
warn()    { echo -e "${YELLOW}WARN:${NC} $*"; }
die()     { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }

# ── Detect OS ─────────────────────────────────────────────────────────────────
detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ -f /etc/arch-release ]]; then
        echo "arch"
    elif [[ -f /etc/debian_version ]]; then
        echo "debian"
    else
        echo "unknown"
    fi
}

OS=$(detect_os)
info "Detected OS: $OS"

# ── Install system build dependencies ─────────────────────────────────────────
install_build_deps() {
    case "$OS" in
        debian)
            info "Installing build dependencies (apt)..."
            sudo apt-get update -qq
            sudo apt-get install -y --no-install-recommends \
                curl gcc pkg-config python3-dev
            ;;
        arch)
            info "Installing build dependencies (pacman)..."
            sudo pacman -Sy --noconfirm --needed \
                curl gcc pkgconf python
            ;;
        macos)
            # Xcode CLT provides gcc/clang; pkg-config via Homebrew if available
            if ! xcode-select -p &>/dev/null; then
                info "Installing Xcode Command Line Tools..."
                xcode-select --install || true
                echo "Re-run this script after Xcode CLT installation completes."
                exit 0
            fi
            if command -v brew &>/dev/null && ! command -v pkg-config &>/dev/null; then
                info "Installing pkg-config via Homebrew..."
                brew install pkg-config
            fi
            ;;
        *)
            warn "Unknown OS — skipping system dep install. Ensure gcc, pkg-config, python3-dev are present."
            ;;
    esac
}

# ── Install Rust via rustup ────────────────────────────────────────────────────
install_rust() {
    info "Installing Rust via rustup..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --profile minimal
    # Source cargo env for the rest of this script
    # shellcheck source=/dev/null
    source "$HOME/.cargo/env"
    info "Rust installed: $(rustc --version)"
}

ensure_rust() {
    if command -v cargo &>/dev/null; then
        info "cargo found: $(cargo --version)"
        # Make sure env is sourced if installed via rustup
        [[ -f "$HOME/.cargo/env" ]] && source "$HOME/.cargo/env"
        return
    fi

    case "$OS" in
        arch)
            # Arch has rust in the official repos — prefer that over rustup
            info "Installing Rust via pacman..."
            sudo pacman -Sy --noconfirm --needed rust
            ;;
        macos)
            if command -v brew &>/dev/null; then
                info "Installing Rust via Homebrew..."
                brew install rust
            else
                install_rust
            fi
            ;;
        *)
            install_rust
            ;;
    esac

    command -v cargo &>/dev/null || die "cargo still not found after install. Check your PATH."
    info "Rust ready: $(rustc --version)"
}

# ── Install maturin ────────────────────────────────────────────────────────────
ensure_maturin() {
    if command -v maturin &>/dev/null; then
        info "maturin found: $(maturin --version)"
        return
    fi

    info "Installing maturin..."

    # Try pip / pip3 in order
    if command -v pip &>/dev/null; then
        pip install maturin --no-cache-dir
    elif command -v pip3 &>/dev/null; then
        pip3 install maturin --no-cache-dir
    else
        die "pip not found. Install Python pip first."
    fi

    command -v maturin &>/dev/null || die "maturin still not found after install."
    info "maturin ready: $(maturin --version)"
}

# ── Build ──────────────────────────────────────────────────────────────────────
build_extension() {
    info "Building p2_s3_crypto (release)..."
    mkdir -p "$OUT_DIR"
    cd "$EXT_DIR"
    maturin build --release --out "$OUT_DIR"

    SO=$(find "$OUT_DIR" -name "p2_s3_crypto*.so" | head -1)
    [[ -z "$SO" ]] && die "No .so found in $OUT_DIR after build."

    cp "$SO" "$DEST/p2_s3_crypto.so"
    info "Installed: $DEST/p2_s3_crypto.so"
}

# ── Main ───────────────────────────────────────────────────────────────────────
install_build_deps
ensure_rust
ensure_maturin
build_extension

echo ""
echo -e "${GREEN}Done.${NC} Commit p2/s3/p2_s3_crypto.so and run: docker compose up"
