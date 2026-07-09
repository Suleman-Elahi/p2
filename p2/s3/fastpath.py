"""Shared helpers for raw S3 data-plane handlers.

Keep this module free of Django view dependencies so ASGI/RSGI fast paths can
enforce the same safety checks without routing through the full middleware stack.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
from typing import Any

from p2.core.acl import has_volume_permission
from p2.s3.auth.aws_v4 import UNSIGNED_PAYLOAD
from p2.s3.errors import (
    AWSAccessDenied,
    AWSBadDigest,
    AWSContentSignatureMismatch,
    AWSIncompleteBody,
    AWSInvalidDigest,
)
from p2.s3.policy import AccessCheckResult, check_access, parse_policy

LOGGER = logging.getLogger(__name__)

_PERM_TO_S3_ACTION = {
    "read": "s3:GetObject",
    "list": "s3:ListBucket",
    "write": "s3:PutObject",
    "delete": "s3:DeleteObject",
    "admin": "s3:PutBucketPolicy",
}


async def _public_policy_allows(volume, permission: str, bucket_name: str, object_key: str) -> bool:
    policy_json = (volume.tags or {}).get("s3.p2.io/bucket-policy")
    if not policy_json:
        return False
    action = _PERM_TO_S3_ACTION.get(permission)
    if not action:
        return False
    try:
        statements = parse_policy(policy_json)
    except Exception:
        return False
    resource = (
        f"arn:aws:s3:::{bucket_name}/{object_key.lstrip('/')}"
        if object_key else f"arn:aws:s3:::{bucket_name}"
    )
    public_statements = [
        s for s in statements
        if s.get("principal") == "*" or s.get("principal") == {"AWS": "*"}
    ]
    return check_access(public_statements, action, resource) == AccessCheckResult.ALLOW


async def require_volume_permission(user, volume, permission: str, bucket_name: str, object_key: str = "") -> None:
    if await has_volume_permission(user, volume, permission):
        return
    if await _public_policy_allows(volume, permission, bucket_name, object_key):
        return
    raise AWSAccessDenied


def _b64_md5_from_hex(md5_hex: str) -> str:
    try:
        return base64.b64encode(binascii.unhexlify(md5_hex)).decode("ascii")
    except (binascii.Error, ValueError) as exc:
        raise AWSInvalidDigest from exc


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def validate_fast_put_integrity(
    headers: dict[str, str],
    body: bytes,
    final_md5_hex: str,
    final_sha256_hex: str,
    blob_size: int = -1,
) -> None:
    """Validate integrity headers for fast-path PUT."""
    has_body = len(body) > 0
    size = blob_size if blob_size >= 0 else len(body)

    decoded_len = headers.get("x-amz-decoded-content-length")
    if decoded_len is not None:
        try:
            if int(decoded_len) != size:
                raise AWSIncompleteBody
        except ValueError as exc:
            raise AWSIncompleteBody from exc

    sha256_header = headers.get("x-amz-content-sha256")
    if (
        sha256_header
        and sha256_header != UNSIGNED_PAYLOAD
        and not sha256_header.startswith("STREAMING-")
        and not _constant_time_equal(sha256_header, final_sha256_hex)
    ):
        raise AWSContentSignatureMismatch

    expected_md5 = headers.get("content-md5")
    if expected_md5:
        if len(expected_md5) < 24:
            raise AWSInvalidDigest
        if not _constant_time_equal(expected_md5, _b64_md5_from_hex(final_md5_hex)):
            raise AWSBadDigest

    if has_body:
        expected_crc32 = headers.get("x-amz-checksum-crc32")
        if expected_crc32:
            crc32_b64 = base64.b64encode(
                (binascii.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big", signed=False)
            ).decode("ascii")
            if not _constant_time_equal(expected_crc32, crc32_b64):
                raise AWSBadDigest

        expected_sha1 = headers.get("x-amz-checksum-sha1")
        if expected_sha1:
            sha1_b64 = base64.b64encode(hashlib.sha1(body).digest()).decode("ascii")
            if not _constant_time_equal(expected_sha1, sha1_b64):
                raise AWSBadDigest

    expected_sha256 = headers.get("x-amz-checksum-sha256")
    if expected_sha256:
        sha256_b64 = base64.b64encode(binascii.unhexlify(final_sha256_hex)).decode("ascii")
        if not _constant_time_equal(expected_sha256, sha256_b64):
            raise AWSBadDigest


async def existing_object_state(engine, key: str) -> tuple[str | None, int, bool]:
    """Return (metadata_json, existing_size, existing_counted) for key.

    Works with both new block-based schema (``size`` field) and legacy
    ``internal_path`` schema (``blob.p2.io/size`` / ATTR_BLOB_SIZE_BYTES).
    """
    from p2.core.constants import ATTR_BLOB_IS_FOLDER, ATTR_BLOB_SIZE_BYTES

    metadata_json = await asyncio.to_thread(engine.get, key)
    if not metadata_json:
        return None, 0, False
    try:
        attributes: dict[str, Any] = json.loads(metadata_json)
    except (TypeError, ValueError):
        return metadata_json, 0, False

    is_folder = attributes.get("is_folder", attributes.get(ATTR_BLOB_IS_FOLDER, False))
    if is_folder:
        return metadata_json, 0, False

    size = int(
        attributes.get("size", 0)
        or attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
        or 0
    )
    return metadata_json, size, True


async def update_volume_stats_for_put(volume, existing_counted: bool, existing_size: int, new_size: int) -> None:
    from p2.core.volume_stats import adjust_volume_stats
    await adjust_volume_stats(
        volume,
        object_delta=0 if existing_counted else 1,
        bytes_delta=new_size - existing_size,
    )
