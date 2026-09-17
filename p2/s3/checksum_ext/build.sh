#!/usr/bin/env bash
# Build the p2_s3_checksum PyO3 extension and install it into the project venv.
#
# See rust_ext/build.sh for why this installs into the venv instead of copying a
# .so into p2/s3/ — two loadable copies of one extension drift apart silently.
#
# Requires: cargo, maturin, uv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/../../../.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "ERROR: no venv at $VENV — create it first (uv venv)." >&2
    exit 1
fi

echo "Building p2_s3_checksum Rust extension..."
rm -rf dist/
# Must use the same interpreter as scripts/build_rust_ext.sh — see the note in
# rust_ext/build.sh about PyO3 build-config fingerprinting.
maturin build --release --interpreter "$VENV/bin/python" --out dist/

WHEEL=$(find dist/ -name "p2_s3_checksum-*.whl" | head -1)
if [ -z "$WHEEL" ]; then
    echo "ERROR: no wheel found in dist/" >&2
    exit 1
fi

echo "Installing $WHEEL into $VENV ..."
VIRTUAL_ENV="$VENV" uv pip install --reinstall --no-deps "$WHEEL"

P2_REQUIRE_NATIVE=1 "$VENV/bin/python" -c "
import p2_s3_checksum
print('Installed:', p2_s3_checksum.__file__)
"
echo "Done. Import with: from p2.s3._native import checksum"
