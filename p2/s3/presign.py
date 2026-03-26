"""p2 S3 Presigned URL generation and validation.

Pure stdlib implementation using HMAC-SHA256 + base64 — no extra dependencies.
Token format: base64url(<json_payload>).<hmac_signature>
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode

from django.conf import settings

from p2.s3.constants import PRESIGNED_MAX_EXPIRY
from p2.s3.errors import AWSExpiredToken, AWSPresignedInvalid

LOGGER = logging.getLogger(__name__)


def _sign(payload_b64: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return sig


def _b64(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode()).decode().rstrip('=')


def _unb64(data: str) -> str:
    # Re-add padding
    pad = 4 - len(data) % 4
    return base64.urlsafe_b64decode(data + '=' * pad).decode()


def generate_presigned_url(
    base_url: str,
    bucket: str,
    key: str,
    method: str,
    expires_in: int = 3600,
) -> str:
    """Return a presigned URL valid for *expires_in* seconds (max 7 days)."""
    expires_in = min(expires_in, PRESIGNED_MAX_EXPIRY)
    payload = json.dumps({
        "b": bucket,
        "k": key,
        "m": method.upper(),
        "exp": int(time.time()) + expires_in,
    }, separators=(',', ':'))
    payload_b64 = _b64(payload)
    sig = _sign(payload_b64, settings.SECRET_KEY)
    token = f"{payload_b64}.{sig}"
    qs = urlencode({"X-P2-Signature": token, "X-Amz-Expires": expires_in})
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{qs}"


def validate_presigned_token(
    token: str,
    bucket: str,
    key: str,
    method: str,
    max_age: Optional[int] = None,
) -> bool:
    """Validate a presigned token. Raises AWSExpiredToken or AWSPresignedInvalid on failure."""
    try:
        payload_b64, sig = token.rsplit('.', 1)
    except ValueError:
        raise AWSPresignedInvalid

    expected_sig = _sign(payload_b64, settings.SECRET_KEY)
    if not hmac.compare_digest(sig, expected_sig):
        raise AWSPresignedInvalid

    try:
        payload = json.loads(_unb64(payload_b64))
    except (ValueError, UnicodeDecodeError):
        raise AWSPresignedInvalid

    if int(time.time()) > payload.get("exp", 0):
        raise AWSExpiredToken

    if payload.get("b") != bucket or payload.get("k") != key:
        raise AWSPresignedInvalid
    if payload.get("m") != method.upper():
        raise AWSPresignedInvalid

    return True
