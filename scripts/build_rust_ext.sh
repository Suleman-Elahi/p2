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

    if command -v uv &>/dev/null; then
        uv tool install maturin
    elif command -v pip &>/dev/null; then
        pip install maturin --no-cache-dir
    elif command -v pip3 &>/dev/null; then
        pip3 install maturin --no-cache-dir
    elif command -v cargo &>/dev/null; then
        cargo install maturin
    else
        die "No package manager found to install maturin. Install uv, pip, or cargo."
    fi

    # uv tool install puts it in ~/.local/bin; cargo install puts it in ~/.cargo/bin
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v maturin &>/dev/null || die "maturin still not found after install."
    info "maturin ready: $(maturin --version)"
}

# ── Build ──────────────────────────────────────────────────────────────────────
# Target Python 3.12 (Docker) by default. Override with: PYTHON_TARGET=python3.13 ./scripts/build_rust_ext.sh
PYTHON_TARGET="${PYTHON_TARGET:-python3.12}"

ensure_target_python() {
    if command -v "$PYTHON_TARGET" &>/dev/null; then
        info "Target Python: $($PYTHON_TARGET --version)"
        return
    fi

    # Use uv to fetch the target Python version
    local ver="${PYTHON_TARGET#python}"
    if command -v uv &>/dev/null; then
        info "Installing Python $ver via uv..."
        uv python install "$ver"
        PYTHON_TARGET="$(uv python find "$ver")"
        info "Target Python: $($PYTHON_TARGET --version)"
        return
    fi

    die "Python $ver not found and uv not available to install it."
}

build_extension() {
    local name="$1"
    local ext_dir="$2"
    local out_dir="$ext_dir/dist"

    info "Building $name (release) for $($PYTHON_TARGET --version)..."
    rm -rf "$out_dir"
    mkdir -p "$out_dir"
    cd "$ext_dir"
    maturin build --release --interpreter "$PYTHON_TARGET" --out "$out_dir"

    # maturin outputs a .whl — extract the .so from it
    WHL=$(find "$out_dir" -name "${name}*.whl" | head -1)
    if [[ -n "$WHL" ]]; then
        unzip -o -j "$WHL" "*.so" -d "$out_dir" 2>/dev/null || true
    fi

    SO=$(find "$out_dir" -name "${name}*.so" | head -1)
    [[ -z "$SO" ]] && die "No .so found in $out_dir after build."

    cp "$SO" "$DEST/${name}.so"
    info "Installed: $DEST/${name}.so"
}

# ── Main ───────────────────────────────────────────────────────────────────────
install_build_deps
ensure_rust
ensure_maturin
ensure_target_python
build_extension "p2_s3_crypto"   "$REPO_ROOT/p2/s3/rust_ext"
build_extension "p2_s3_checksum" "$REPO_ROOT/p2/s3/checksum_ext"

echo ""
echo -e "${GREEN}Done.${NC} Commit the .so files and run: docker compose up"
echo "  git add p2/s3/p2_s3_crypto.so p2/s3/p2_s3_checksum.so"
