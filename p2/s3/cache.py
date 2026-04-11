"""In-memory LRU caches for hot S3 auth/metadata paths.

These caches eliminate database round-trips for repeated requests to the same
bucket/key combinations. TTL is short (60s) to balance freshness vs performance.
"""
import time
from typing import Optional, Tuple, Dict, Any
from django.conf import settings

# Simple TTL cache for API keys: access_key -> (secret_key, user_id, username, is_superuser, expires_at)
_apikey_cache: dict[str, Tuple[str, int, str, bool, float]] = {}
_APIKEY_TTL = float(getattr(settings, "S3_CACHE_APIKEY_TTL_SECONDS", 600.0))

# Volume cache: bucket_name -> (Volume, expires_at)
_volume_cache: dict[str, Tuple[Any, float]] = {}
_VOLUME_TTL = float(getattr(settings, "S3_CACHE_VOLUME_TTL_SECONDS", 600.0))

# ACL cache: (user_id, volume_pk, permission) -> (allowed, expires_at)
_acl_cache: dict[Tuple[int, str, str], Tuple[bool, float]] = {}
_ACL_TTL = float(getattr(settings, "S3_CACHE_ACL_TTL_SECONDS", 600.0))

# Metadata cache: (volume_uuid_hex, path) -> (attributes_dict, expires_at)
_metadata_cache: dict[Tuple[str, str], Tuple[Dict[str, Any], float]] = {}
_METADATA_TTL = float(getattr(settings, "S3_CACHE_METADATA_TTL_SECONDS", 60.0))
_METADATA_MAX_SIZE = 10000  # Max entries to prevent memory bloat

# Volume permission cache: (user_id, bucket_name, permission) -> (allowed, expires_at)
_volume_perm_cache: dict[Tuple[int, str, str], Tuple[bool, float]] = {}
_VOLUME_PERM_TTL = float(getattr(settings, "S3_CACHE_VOLUME_PERMISSION_TTL_SECONDS", 600.0))


def get_cached_apikey(access_key: str) -> Optional[Tuple[str, int, str, bool]]:
    """Return (secret_key, user_id, username, is_superuser) if cached and not expired."""
    entry = _apikey_cache.get(access_key)
    if entry and entry[4] > time.monotonic():
        return (entry[0], entry[1], entry[2], entry[3])
    return None


def set_cached_apikey(access_key: str, secret_key: str, user_id: int, username: str, is_superuser: bool):
    """Cache an API key lookup result."""
    _apikey_cache[access_key] = (secret_key, user_id, username, is_superuser, time.monotonic() + _APIKEY_TTL)


def get_cached_volume(bucket_name: str) -> Optional[Any]:
    """Return Volume instance if cached."""
    entry = _volume_cache.get(bucket_name)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def set_cached_volume(bucket_name: str, volume: Any):
    """Cache a volume lookup result."""
    _volume_cache[bucket_name] = (volume, time.monotonic() + _VOLUME_TTL)


def get_cached_acl(user_id: int, volume_pk: str, permission: str) -> Optional[bool]:
    """Return cached ACL result if available."""
    entry = _acl_cache.get((user_id, volume_pk, permission))
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def set_cached_acl(user_id: int, volume_pk: str, permission: str, allowed: bool):
    """Cache an ACL check result."""
    _acl_cache[(user_id, volume_pk, permission)] = (allowed, time.monotonic() + _ACL_TTL)


def invalidate_volume(bucket_name: str):
    """Call when a volume is modified."""
    _volume_cache.pop(bucket_name, None)


def invalidate_apikey(access_key: str):
    """Call when an API key is modified."""
    _apikey_cache.pop(access_key, None)


def invalidate_acl(volume_pk: str):
    """Call when ACLs for a volume change."""
    to_remove = [k for k in _acl_cache if k[1] == volume_pk]
    for k in to_remove:
        _acl_cache.pop(k, None)


def get_cached_metadata(volume_uuid_hex: str, path: str) -> Optional[Dict[str, Any]]:
    """Return cached metadata dict if available and not expired."""
    entry = _metadata_cache.get((volume_uuid_hex, path))
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def set_cached_metadata(volume_uuid_hex: str, path: str, attributes: Dict[str, Any]):
    """Cache metadata for a blob."""
    # Simple size limit - evict oldest entries if too large
    if len(_metadata_cache) >= _METADATA_MAX_SIZE:
        # Remove ~10% of entries (oldest by expiry)
        to_remove = sorted(_metadata_cache.items(), key=lambda x: x[1][1])[:_METADATA_MAX_SIZE // 10]
        for key, _ in to_remove:
            _metadata_cache.pop(key, None)
    
    _metadata_cache[(volume_uuid_hex, path)] = (attributes, time.monotonic() + _METADATA_TTL)


def invalidate_metadata(volume_uuid_hex: str, path: str):
    """Invalidate cached metadata for a specific blob."""
    _metadata_cache.pop((volume_uuid_hex, path), None)


def invalidate_volume_metadata(volume_uuid_hex: str):
    """Invalidate all cached metadata for a volume."""
    to_remove = [k for k in _metadata_cache if k[0] == volume_uuid_hex]
    for k in to_remove:
        _metadata_cache.pop(k, None)


def clear_all_caches():
    """Clear all caches. Useful after major changes like recreating storage/volumes."""
    _apikey_cache.clear()
    _volume_cache.clear()
    _acl_cache.clear()
    _metadata_cache.clear()
    _volume_perm_cache.clear()


def get_cached_volume_permission(user_id: int, bucket_name: str, permission: str) -> Optional[bool]:
    """Return cached bucket permission result if available and not expired."""
    entry = _volume_perm_cache.get((user_id, bucket_name, permission))
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def set_cached_volume_permission(user_id: int, bucket_name: str, permission: str, allowed: bool):
    """Cache bucket permission result for a user."""
    _volume_perm_cache[(user_id, bucket_name, permission)] = (allowed, time.monotonic() + _VOLUME_PERM_TTL)
