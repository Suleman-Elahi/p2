#!/usr/bin/env bash
# Build the p2_s3_crypto PyO3 extension and copy it into the p2/s3/ package.
# Requires: cargo, maturin (pip install maturin)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Building p2_s3_crypto Rust extension..."
maturin build --release --out dist/

# Find the built .so and copy it next to presign.py
SO=$(find dist/ -name "p2_s3_crypto*.so" | head -1)
if [ -z "$SO" ]; then
    echo "ERROR: no .so found in dist/"
    exit 1
fi

cp "$SO" "../p2_s3_crypto.so"
echo "Installed: ../p2_s3_crypto.so"
echo "Done. Import with: from p2.s3 import p2_s3_crypto"
