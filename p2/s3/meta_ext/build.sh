#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Check if rustc is available
if ! command -v rustc &> /dev/null; then
    echo "Rust is not installed. Please install it first."
    exit 1
fi

# Check if maturin is available
if ! command -v maturin &> /dev/null; then
    echo "Installing maturin..."
    uv tool install maturin
fi

echo "Building p2_s3_meta extension..."
uv tool run maturin build --release

# Find the built wheel and install it / extract the .so
WHEEL=$(ls target/wheels/*.whl | head -n 1)
echo "Extracting extension from $WHEEL"

unzip -q -o "$WHEEL" "p2_s3_meta/p2_s3_meta.abi3.so" -d /tmp/p2_s3_meta_extracted || unzip -q -o "$WHEEL" "*.so" -d /tmp/p2_s3_meta_extracted
find /tmp/p2_s3_meta_extracted -name "*.so" -exec cp {} ../p2_s3_meta.so \;
rm -rf /tmp/p2_s3_meta_extracted

echo "Successfully built and copied p2_s3_meta.so to p2/s3/"
