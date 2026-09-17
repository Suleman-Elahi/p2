#!/usr/bin/env bash
# Build the p2_s3_crypto PyO3 extension and install it into the project venv.
#
# The extension is imported as a top-level module (see p2/s3/_native.py), so the
# venv is the single source of truth. This script used to also copy the .so to
# p2/s3/p2_s3_crypto.so, which created a second, independently-loadable copy of
# the same extension; the two drifted and the tree copy went stale while callers
# using a different import spelling kept picking it up. Do not reintroduce that.
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

echo "Building p2_s3_crypto Rust extension..."
rm -rf dist/
# Build against the venv interpreter. PyO3 keys its build config on the
# interpreter path, so using a different one here than scripts/build_rust_ext.sh
# would invalidate the cargo fingerprint and force a full ~22s rebuild on every
# alternation instead of a ~0.4s cache hit. Both scripts must agree.
maturin build --release --interpreter "$VENV/bin/python" --out dist/

WHEEL=$(find dist/ -name "p2_s3_crypto-*.whl" | head -1)
if [ -z "$WHEEL" ]; then
    echo "ERROR: no wheel found in dist/" >&2
    exit 1
fi

echo "Installing $WHEEL into $VENV ..."
VIRTUAL_ENV="$VENV" uv pip install --reinstall --no-deps "$WHEEL"

# Fail loudly here rather than degrading to a slow Python path at runtime.
P2_REQUIRE_NATIVE=1 "$VENV/bin/python" -c "
import p2_s3_crypto
print('Installed:', p2_s3_crypto.__file__)
"
echo "Done. Import with: from p2.s3._native import crypto"
