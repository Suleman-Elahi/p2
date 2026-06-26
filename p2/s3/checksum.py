"""S3 payload checksum verification.

Uses the Rust p2_s3_checksum extension when available, falls back to
pure-Python implementations.

AWS SDKs send checksums via x-amz-checksum-{crc32,crc32c,sha256,sha1}
headers and declare the algorithm in x-amz-sdk-checksum-algorithm.
"""
import hashlib
import logging
import struct
from base64 import b64encode

LOGGER = logging.getLogger(__name__)

try:
    from p2.s3 import p2_s3_checksum as _rust
    _HAS_RUST = True
    LOGGER.debug("Using Rust p2_s3_checksum extension")
except ImportError:
    _rust = None
    _HAS_RUST = False
    LOGGER.debug("Rust p2_s3_checksum not available, using Python fallback")


def _py_compute_crc32c(data: bytes) -> str:
    """Pure-Python CRC32C (Castagnoli) using binascii with polynomial 0x1EDC6F41.

    CRC32C is the same as CRC32 but with a different polynomial.
    Python's binascii.crc32 only supports the standard CRC32 polynomial,
    so we fall back to the crc32c algorithm implemented manually.
    """
    # CRC32C lookup table (Castagnoli polynomial 0x1EDC6F41)
    _crc32c_table = None

    def _make_crc32c_table():
        nonlocal _crc32c_table
        if _crc32c_table is not None:
            return _crc32c_table
        table = []
        for i in range(256):
            crc = i
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0x82F63B78
                else:
                    crc >>= 1
            table.append(crc)
        _crc32c_table = table
        return table

    table = _make_crc32c_table()
    crc = 0xFFFFFFFF
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    crc ^= 0xFFFFFFFF
    return b64encode(struct.pack(">I", crc)).decode()


def _py_compute_crc32(data: bytes) -> str:
    import binascii
    return b64encode(struct.pack(">I", binascii.crc32(data) & 0xFFFFFFFF)).decode()


def _py_compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _py_compute_sha1(data: bytes) -> str:
    return b64encode(hashlib.sha1(data).digest()).decode()


# Dispatch table: algorithm name → (header_name, compute_fn)
_ALGORITHMS = {}


def _init():
    global _ALGORITHMS
    if _HAS_RUST:
        _ALGORITHMS = {
            "CRC32": ("HTTP_X_AMZ_CHECKSUM_CRC32", _rust.verify_crc32, _rust.compute_crc32),
            "CRC32C": ("HTTP_X_AMZ_CHECKSUM_CRC32C", _rust.verify_crc32c, _rust.compute_crc32c),
            "SHA256": ("HTTP_X_AMZ_CHECKSUM_SHA256", _rust.verify_sha256, _rust.compute_sha256),
            "SHA1": ("HTTP_X_AMZ_CHECKSUM_SHA1", _rust.verify_sha1, _rust.compute_sha1),
        }
    else:
        _ALGORITHMS = {
            "CRC32": ("HTTP_X_AMZ_CHECKSUM_CRC32", None, _py_compute_crc32),
            "CRC32C": ("HTTP_X_AMZ_CHECKSUM_CRC32C", None, _py_compute_crc32c),
            "SHA256": ("HTTP_X_AMZ_CHECKSUM_SHA256", None, _py_compute_sha256),
            "SHA1": ("HTTP_X_AMZ_CHECKSUM_SHA1", None, _py_compute_sha1),
        }


_init()


def verify_request_checksum(request, body: bytes) -> str | None:
    """Verify the payload checksum from request headers.

    Returns an error message string if verification fails, None if OK or
    no checksum header was sent.
    """
    for algo, (header, verify_fn, compute_fn) in _ALGORITHMS.items():
        expected = request.META.get(header)
        if not expected:
            continue
        if verify_fn:
            if not verify_fn(body, expected):
                computed = compute_fn(body)
                return (f"Value for x-amz-checksum-{algo.lower()} header is invalid. "
                        f"Expected {expected}, got {computed}")
        else:
            computed = compute_fn(body)
            if computed != expected:
                return (f"Value for x-amz-checksum-{algo.lower()} header is invalid. "
                        f"Expected {expected}, got {computed}")
        return None  # matched and verified
    return None  # no checksum header present
