"""Tests for p2.s3.checksum — payload checksum verification."""
import hashlib
import struct
from base64 import b64encode

import pytest

from p2.s3.checksum import (
    _py_compute_crc32,
    _py_compute_sha1,
    _py_compute_sha256,
    verify_request_checksum,
)


class FakeRequest:
    def __init__(self, **meta):
        self.META = meta


class TestPythonFallbacks:

    def test_crc32_empty(self):
        assert _py_compute_crc32(b"") == "AAAAAA=="

    def test_crc32_hello(self):
        import binascii
        expected = b64encode(struct.pack(">I", binascii.crc32(b"hello") & 0xFFFFFFFF)).decode()
        assert _py_compute_crc32(b"hello") == expected

    def test_sha256_empty(self):
        assert _py_compute_sha256(b"") == hashlib.sha256(b"").hexdigest()

    def test_sha256_data(self):
        data = b"test payload"
        assert _py_compute_sha256(data) == hashlib.sha256(data).hexdigest()

    def test_sha1_empty(self):
        assert _py_compute_sha1(b"") == b64encode(hashlib.sha1(b"").digest()).decode()

    def test_sha1_data(self):
        data = b"test payload"
        assert _py_compute_sha1(data) == b64encode(hashlib.sha1(data).digest()).decode()


class TestVerifyRequestChecksum:

    def test_no_checksum_header(self):
        req = FakeRequest()
        assert verify_request_checksum(req, b"anything") is None

    def test_crc32_match(self):
        data = b"hello world"
        req = FakeRequest(HTTP_X_AMZ_CHECKSUM_CRC32=_py_compute_crc32(data))
        assert verify_request_checksum(req, data) is None

    def test_crc32_mismatch(self):
        req = FakeRequest(HTTP_X_AMZ_CHECKSUM_CRC32=_py_compute_crc32(b"right"))
        result = verify_request_checksum(req, b"wrong")
        assert result is not None
        assert "invalid" in result.lower()

    def test_sha256_match(self):
        data = b"test data"
        req = FakeRequest(HTTP_X_AMZ_CHECKSUM_SHA256=_py_compute_sha256(data))
        assert verify_request_checksum(req, data) is None

    def test_sha256_mismatch(self):
        req = FakeRequest(HTTP_X_AMZ_CHECKSUM_SHA256="0000000000000000000000000000000000000000000000000000000000000000")
        result = verify_request_checksum(req, b"data")
        assert result is not None

    def test_sha1_match(self):
        data = b"test"
        req = FakeRequest(HTTP_X_AMZ_CHECKSUM_SHA1=_py_compute_sha1(data))
        assert verify_request_checksum(req, data) is None

    def test_first_header_wins(self):
        """If multiple checksum headers present, first match is used."""
        data = b"test"
        req = FakeRequest(
            HTTP_X_AMZ_CHECKSUM_CRC32=_py_compute_crc32(data),
            HTTP_X_AMZ_CHECKSUM_SHA256="wrong",
        )
        # CRC32 is checked first and matches, so result is None
        assert verify_request_checksum(req, data) is None
