#!/usr/bin/env bash
# Build both Rust extensions (p2_s3_crypto, p2_s3_checksum) and install them
# into the project venv.
#
# Automatically installs Rust (via rustup) and maturin if not present.
# Supported: Debian/Ubuntu, Arch Linux, macOS (Homebrew or standalone rustup).
#
# Run whenever p2/s3/rust_ext/ or p2/s3/checksum_ext/ changes.
#
# This script used to copy each .so into p2/s3/ and instruct you to commit it,
# while run_without_docker.sh separately ran `maturin develop` into the venv.
# Two destinations meant two loadable copies of the same extension: the tree
# copy went stale (missing write_blocks_uring) while callers using a different
# import spelling kept loading it, and nothing failed because every call site
# swallowed ImportError. The venv is now the only destination. See p2/s3/_native.py.
#
# The prebuilt wheels in wheels/ are the primary install source for both this
# native script and the Docker image, so neither needs a Rust toolchain for a
# normal run. Rust is only bootstrapped below when a wheel is missing or older
# than the Rust sources (i.e. someone edited p2/s3/*_ext), or FORCE_REBUILD=1.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
WHEELHOUSE="$REPO_ROOT/wheels"

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
    # Skip the package manager entirely when the toolchain is already present.
    # This script runs on every `run_without_docker.sh` start, and an
    # unconditional `sudo apt-get update` both costs several seconds and can
    # block on a password prompt before the server comes up.
    if command -v gcc &>/dev/null \
        && command -v pkg-config &>/dev/null \
        && command -v curl &>/dev/null; then
        info "Build dependencies already present — skipping package manager."
        return
    fi

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
        if [[ -f "$HOME/.cargo/env" ]]; then
            source "$HOME/.cargo/env"
        fi
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
# Default to the venv interpreter, NOT a bare `python3.12` from PATH.
#
# PyO3's build config is keyed on the interpreter path, so building with a
# different interpreter invalidates the cargo fingerprint and forces a full
# rebuild of pyo3 + the crate (~22s) instead of a cache hit (~0.4s). The venv
# interpreter is also the one that actually loads the extension at runtime, so
# it is the correct target. Override with:
#   PYTHON_TARGET=python3.13 bash scripts/build_rust_ext.sh
if [[ -z "${PYTHON_TARGET:-}" && -x "$VENV/bin/python" ]]; then
    PYTHON_TARGET="$VENV/bin/python"
fi
PYTHON_TARGET="${PYTHON_TARGET:-python3.12}"

ensure_target_python() {
    if command -v "$PYTHON_TARGET" &>/dev/null; then
        info "Target Python: $($PYTHON_TARGET --version) ($PYTHON_TARGET)"
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

# Return 0 when the installed extension is newer than every relevant source
# file, i.e. there is nothing to do. Set FORCE_REBUILD=1 to always rebuild.
#
# Without this, every server start reinstalled the wheel even when no Rust
# changed. Cargo would cache the compile, but the uninstall/reinstall churn
# still ran and made it look like a full rebuild each time.
ext_is_current() {
    local name="$1"
    local ext_dir="$2"

    [[ -n "${FORCE_REBUILD:-}" ]] && return 1
    [[ -x "$VENV/bin/python" ]] || return 1

    local installed
    installed="$("$VENV/bin/python" -c "
import importlib.util, pathlib, sys
spec = importlib.util.find_spec('$name')
if spec is None or not spec.origin:
    sys.exit(1)
so = sorted(pathlib.Path(spec.origin).parent.glob('*.so'))
if not so:
    sys.exit(1)
print(so[0], end='')
" 2>/dev/null)" || return 1
    [[ -n "$installed" && -f "$installed" ]] || return 1

    # Any tracked source newer than the installed binary means it is stale.
    local newer
    newer="$(find "$ext_dir" \
        \( -path '*/target' -o -path '*/dist' \) -prune -o \
        \( -name '*.rs' -o -name 'Cargo.toml' -o -name 'Cargo.lock' \
           -o -name '*.pyi' -o -name 'config.toml' -o -name 'build.sh' \) \
        -newer "$installed" -print -quit 2>/dev/null)"

    [[ -z "$newer" ]]
}

# ── Prebuilt wheels ───────────────────────────────────────────────────────────
# Path to the newest committed wheel for an extension, or empty if none.
find_wheel() {
    local name="$1"
    ls -1 "$WHEELHOUSE/${name}"-*.whl 2>/dev/null | sort | tail -1
}

# A wheel is usable as-is when it exists and no tracked Rust source is newer
# than it. If a source is newer the wheel is stale and must be rebuilt.
wheel_is_current() {
    local name="$1"
    local ext_dir="$2"

    [[ -n "${FORCE_REBUILD:-}" ]] && return 1

    local whl
    whl="$(find_wheel "$name")"
    [[ -n "$whl" && -f "$whl" ]] || return 1

    local newer
    newer="$(find "$ext_dir" \
        \( -path '*/target' -o -path '*/dist' \) -prune -o \
        \( -name '*.rs' -o -name 'Cargo.toml' -o -name 'Cargo.lock' \
           -o -name '*.pyi' -o -name 'config.toml' -o -name 'build.sh' \) \
        -newer "$whl" -print -quit 2>/dev/null)"

    [[ -z "$newer" ]]
}

install_prebuilt_wheel() {
    local name="$1"
    local whl="$2"
    info "Installing prebuilt wheel for $name: $(basename "$whl")"
    if command -v uv &>/dev/null; then
        VIRTUAL_ENV="$VENV" uv pip install --reinstall --no-deps "$whl"
    else
        "$VENV/bin/pip" install --force-reinstall --no-deps "$whl"
    fi
}

# Verify a wheel can actually be imported on THIS machine before installing it.
#
# `pip`/`uv` only validate the ABI and platform tags; they cannot see CPU
# instructions baked in through target-cpu=native. Loading such a wheel on an
# older CPU raises SIGILL and kills the process. So extract it to a temp dir and
# import it in a throwaway subprocess — a crash or ImportError means "not
# compatible here" (return non-zero) and the caller falls back to a source build.
wheel_loads_here() {
    local name="$1"
    local whl="$2"
    local tmp rc

    [[ -x "$VENV/bin/python" ]] || return 0  # no interpreter to test with
    [[ -f "$whl" ]] || return 1

    tmp="$(mktemp -d)"
    if ! "$VENV/bin/python" -m zipfile -e "$whl" "$tmp" >/dev/null 2>&1; then
        rm -rf "$tmp"
        return 1
    fi

    P2_REQUIRE_NATIVE=1 "$VENV/bin/python" - "$tmp" "$name" >/dev/null 2>&1 <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
__import__(sys.argv[2])
PY
    rc=$?

    rm -rf "$tmp"
    return $rc
}

build_extension() {
    local name="$1"
    local ext_dir="$2"
    local out_dir="$ext_dir/dist"

    if ext_is_current "$name" "$ext_dir"; then
        info "$name is up to date — skipping build (FORCE_REBUILD=1 to override)."
        return
    fi

    info "Building $name (release) for $($PYTHON_TARGET --version)..."
    rm -rf "$out_dir"
    mkdir -p "$out_dir"
    cd "$ext_dir"
    maturin build --release --interpreter "$PYTHON_TARGET" --out "$out_dir"

    WHL=$(find "$out_dir" -name "${name}-*.whl" | head -1)
    [[ -z "$WHL" ]] && die "No wheel found in $out_dir after build."

    # Install into the venv — the single import source. Never copy a bare .so
    # into p2/s3/; that is what caused the stale-build divergence.
    mkdir -p "$WHEELHOUSE"
    cp "$WHL" "$WHEELHOUSE/"

    if [[ -x "$VENV/bin/python" ]]; then
        VIRTUAL_ENV="$VENV" uv pip install --reinstall --no-deps "$WHL"
        info "Installed into venv: $name"
    else
        warn "No venv at $VENV — wheel staged in wheels/ but not installed."
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────────
# This runs on every native start, so the common case must stay toolchain-free.
# Per extension:
#   1. already installed and newer than the sources -> skip
#   2. a current prebuilt wheel exists               -> install it (no Rust)
#   3. otherwise                                     -> bootstrap Rust + build
#
# Rust is only required when a wheel is missing or older than the Rust sources
# (i.e. someone edited p2/s3/*_ext) or when FORCE_REBUILD=1 is set.
EXTENSIONS=(
    "p2_s3_crypto:$REPO_ROOT/p2/s3/rust_ext"
    "p2_s3_checksum:$REPO_ROOT/p2/s3/checksum_ext"
)

need_source_build=0
for entry in "${EXTENSIONS[@]}"; do
    name="${entry%%:*}"
    ext_dir="${entry#*:}"

    if ext_is_current "$name" "$ext_dir"; then
        info "$name is up to date — skipping."
        continue
    fi

    whl="$(find_wheel "$name")"
    if [[ -n "$whl" ]] && wheel_is_current "$name" "$ext_dir"; then
        info "Checking prebuilt wheel for $name compatibility..."
        if wheel_loads_here "$name" "$whl"; then
            install_prebuilt_wheel "$name" "$whl"
            continue
        fi
        warn "Prebuilt wheel for $name is incompatible with this CPU — falling back to a source build."
    else
        info "$name has no current prebuilt wheel."
    fi

    need_source_build=1
done

if [[ "$need_source_build" == 1 ]]; then
    install_build_deps
    ensure_rust
    ensure_maturin
    ensure_target_python
    for entry in "${EXTENSIONS[@]}"; do
        name="${entry%%:*}"
        ext_dir="${entry#*:}"
        build_extension "$name" "$ext_dir"
    done
fi

# Verify both extensions load and expose the full expected surface. Fails here
# rather than silently degrading to the Python fallback at runtime.
if [[ -x "$VENV/bin/python" ]]; then
    info "Verifying extensions..."
    P2_REQUIRE_NATIVE=1 "$VENV/bin/python" -c "
import p2_s3_crypto, p2_s3_checksum
print('  p2_s3_crypto  :', p2_s3_crypto.__file__)
print('  p2_s3_checksum:', p2_s3_checksum.__file__)
" || die "Extensions built but failed verification."
fi

echo ""
echo -e "${GREEN}Done.${NC} Extensions installed into $VENV"
echo "Wheels in $WHEELHOUSE are the install source for native runs and Docker."
echo ""
warn "NOTE: wheels are abi3 (stable ABI, Python >= 3.12) and forward-compatible"
warn "with 3.13+. rust_ext/.cargo/config.toml pins target-cpu=native, so a wheel"
warn "built here is tuned to THIS CPU and may fault (SIGILL) on an older one."
warn "Native runs and Docker both install these wheels, so rebuild portably"
warn "before shipping them:"
warn "  FORCE_REBUILD=1 RUSTFLAGS='-C target-cpu=x86-64-v2' bash scripts/build_rust_ext.sh"
