#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "Building p2_s3_checksum Rust extension..."
maturin build --release --out dist/
SO=$(find dist/ -name "p2_s3_checksum*.so" | head -1)
if [ -z "$SO" ]; then
    echo "ERROR: no .so found in dist/"
    exit 1
fi
cp "$SO" "../p2_s3_checksum.so"
echo "Installed: ../p2_s3_checksum.so"
echo "Done. Import with: from p2.s3 import p2_s3_checksum"
