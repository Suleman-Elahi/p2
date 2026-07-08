"""Transparent compression for S3 objects.

Provides zlib-based compression for objects larger than a configurable threshold.
Compressed objects are stored with a metadata marker so they can be transparently
decompressed on GET. This reduces storage usage and network transfer for
compressible data (text, JSON, logs, etc.) at the cost of CPU.

Compression is disabled by default. Enable via:
    P2_S3__COMPRESSION__ENABLED=true
    P2_S3__COMPRESSION__MIN_SIZE=1048576  # 1MB default
    P2_S3__COMPRESSION__LEVEL=6           # zlib level 1-9
"""
from __future__ import annotations

import zlib
from typing import Optional

from django.conf import settings

# Compression settings
_ENABLED = bool(getattr(settings, "S3_COMPRESSION_ENABLED", False))
_MIN_SIZE = int(getattr(settings, "S3_COMPRESSION_MIN_SIZE", 1024 * 1024))  # 1MB
_LEVEL = int(getattr(settings, "S3_COMPRESSION_LEVEL", 6))

# Metadata key to mark compressed objects
_COMPRESSED_ATTR = "blob.p2.io/compressed"


def should_compress(size: int) -> bool:
    """Return True if an object of this size should be compressed."""
    return _ENABLED and size >= _MIN_SIZE


def compress(data: bytes) -> tuple[bytes, bool]:
    """Compress data if compression is enabled and beneficial.

    Returns (compressed_data, was_compressed).
    """
    if not _ENABLED or len(data) < _MIN_SIZE:
        return data, False

    try:
        compressed = zlib.compress(data, _LEVEL)
        # Only use compression if it actually saves space (>10% reduction)
        if len(compressed) < len(data) * 0.9:
            return compressed, True
        return data, False
    except Exception:
        return data, False


def decompress(data: bytes, is_compressed: bool) -> bytes:
    """Decompress data if it was compressed.

    Args:
        data: The stored data
        is_compressed: Whether the data was compressed (from metadata)
    """
    if not is_compressed:
        return data

    try:
        return zlib.decompress(data)
    except Exception:
        # If decompression fails, return original data
        return data


def get_compression_metadata(original_size: int, compressed_size: int) -> dict:
    """Return metadata dict for a compressed object."""
    return {
        _COMPRESSED_ATTR: True,
        "blob.p2.io/original_size": original_size,
        "blob.p2.io/compressed_size": compressed_size,
    }


def is_compressed(metadata: dict) -> bool:
    """Check if object metadata indicates compression."""
    return bool(metadata.get(_COMPRESSED_ATTR))
