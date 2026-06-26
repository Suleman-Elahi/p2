"""Type stubs for the p2_s3_checksum Rust extension (PyO3)."""

def verify_crc32(data: bytes, expected_b64: str) -> bool:
    """Verify CRC32 checksum against base64-encoded expected value."""
    ...

def verify_crc32c(data: bytes, expected_b64: str) -> bool:
    """Verify CRC32C (Castagnoli) checksum against base64-encoded expected value."""
    ...

def verify_sha256(data: bytes, expected_hex: str) -> bool:
    """Verify SHA-256 checksum against hex-encoded expected value."""
    ...

def verify_sha1(data: bytes, expected_b64: str) -> bool:
    """Verify SHA-1 checksum against base64-encoded expected value."""
    ...

def compute_crc32(data: bytes) -> str:
    """Compute CRC32 checksum, returns base64-encoded string."""
    ...

def compute_crc32c(data: bytes) -> str:
    """Compute CRC32C (Castagnoli) checksum, returns base64-encoded string."""
    ...

def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 checksum, returns lowercase hex string."""
    ...

def compute_sha1(data: bytes) -> str:
    """Compute SHA-1 checksum, returns base64-encoded string."""
    ...
