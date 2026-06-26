"""In-memory LRU+TTL caches for hot S3 auth/metadata paths.

These caches eliminate database round-trips for repeated requests to the same
bucket/key combinations. TTL is short (60s) to balance freshness vs performance.
Metadata cache uses an OrderedDict for O(1) LRU eviction instead of O(n log n) sort.

Cross-worker invalidation
~~~~~~~~~~~~~~~~~~~~~~~~~
Volume objects are cached per-worker in Python dicts, but Granian runs multiple
worker processes.  To propagate config changes (versioning, access policy) across
workers we use Django's Redis cache as a shared generation counter:

    invalidate_volume_global(name)  — bumps a Redis counter
    get_cached_volume(name)         — checks local gen vs Redis gen, evicts on mismatch

One Redis GET per volume-lookup adds <1ms but guarantees instant propagation.
"""
import time
from collections import OrderedDict
from typing import Optional, Tuple, Dict, Any
from django.conf import settings

# Simple TTL cache for API keys: access_key -> (secret_key, user_id, username, is_superuser, expires_at)
_apikey_cache: dict[str, Tuple[str, int, str, bool, float]] = {}
_APIKEY_TTL = float(getattr(settings, "S3_CACHE_APIKEY_TTL_SECONDS", 600.0))

# Volume cache: bucket_name -> (Volume, generation, expires_at)
_volume_cache: dict[str, Tuple[Any, int, float]] = {}
_VOLUME_TTL = float(getattr(settings, "S3_CACHE_VOLUME_TTL_SECONDS", 60.0))

# ACL cache: (user_id, volume_pk, permission) -> (allowed, expires_at)
_acl_cache: dict[Tuple[int, str, str], Tuple[bool, float]] = {}
_ACL_TTL = float(getattr(settings, "S3_CACHE_ACL_TTL_SECONDS", 600.0))

# Metadata cache: (volume_uuid_hex, path) -> (attributes_dict, expires_at)
# OrderedDict preserves insertion order for O(1) LRU eviction (move_to_end + popitem).
_metadata_cache: OrderedDict[Tuple[str, str], Tuple[Dict[str, Any], float]] = OrderedDict()
_METADATA_TTL = float(getattr(settings, "S3_CACHE_METADATA_TTL_SECONDS", 60.0))
_METADATA_MAX_SIZE = 10000  # Max entries to prevent memory bloat

# Volume permission cache: (user_id, bucket_name, permission) -> (allowed, expires_at)
_volume_perm_cache: dict[Tuple[int, str, str], Tuple[bool, float]] = {}
_VOLUME_PERM_TTL = float(getattr(settings, "S3_CACHE_VOLUME_PERMISSION_TTL_SECONDS", 600.0))


# ─── Cross-worker generation counter (Redis-backed) ─────────────────────────

def _redis_gen_key(bucket_name: str) -> str:
    """Redis key for the volume generation counter."""
    return f"p2:vol_gen:{bucket_name}"


def _get_redis_generation(bucket_name: str) -> int:
    """Read the current generation counter from Redis. Returns 0 on miss/error."""
    try:
        from django.core.cache import cache
        val = cache.get(_redis_gen_key(bucket_name))
        return int(val) if val is not None else 0
    except Exception:
        return 0


def _bump_redis_generation(bucket_name: str) -> int:
    """Increment the generation counter in Redis. Returns the new value."""
    try:
        from django.core.cache import cache
        key = _redis_gen_key(bucket_name)
        # Use cache.incr with a fallback for first-time use
        try:
            new_val = cache.incr(key)
        except ValueError:
            # Key doesn't exist yet — initialize it
            cache.set(key, 1, timeout=None)
            new_val = 1
        return new_val
    except Exception:
        return 0


# ─── API Key cache ───────────────────────────────────────────────────────────

def get_cached_apikey(access_key: str) -> Optional[Tuple[str, int, str, bool]]:
    """Return (secret_key, user_id, username, is_superuser) if cached and not expired."""
    entry = _apikey_cache.get(access_key)
    if entry and entry[4] > time.monotonic():
        return (entry[0], entry[1], entry[2], entry[3])
    return None


def set_cached_apikey(access_key: str, secret_key: str, user_id: int, username: str, is_superuser: bool):
    """Cache an API key lookup result."""
    _apikey_cache[access_key] = (secret_key, user_id, username, is_superuser, time.monotonic() + _APIKEY_TTL)


# ─── Volume cache (with cross-worker generation check) ──────────────────────

def get_cached_volume(bucket_name: str) -> Optional[Any]:
    """Return Volume instance if cached and generation matches Redis.

    One Redis GET per call (~0.1ms local) to guarantee cross-worker freshness.
    """
    entry = _volume_cache.get(bucket_name)
    if entry and entry[2] > time.monotonic():
        # Check generation against Redis shared counter
        redis_gen = _get_redis_generation(bucket_name)
        if entry[1] == redis_gen:
            return entry[0]
        # Generation mismatch — evict stale entry
        _volume_cache.pop(bucket_name, None)
    return None


def set_cached_volume(bucket_name: str, volume: Any):
    """Cache a volume lookup result with current Redis generation."""
    gen = _get_redis_generation(bucket_name)
    _volume_cache[bucket_name] = (volume, gen, time.monotonic() + _VOLUME_TTL)


# ─── ACL cache ───────────────────────────────────────────────────────────────

def get_cached_acl(user_id: int, volume_pk: str, permission: str) -> Optional[bool]:
    """Return cached ACL result if available."""
    entry = _acl_cache.get((user_id, volume_pk, permission))
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def set_cached_acl(user_id: int, volume_pk: str, permission: str, allowed: bool):
    """Cache an ACL check result."""
    _acl_cache[(user_id, volume_pk, permission)] = (allowed, time.monotonic() + _ACL_TTL)


# ─── Volume invalidation ────────────────────────────────────────────────────

def invalidate_volume(bucket_name: str):
    """Invalidate volume in THIS worker only (local eviction)."""
    _volume_cache.pop(bucket_name, None)


def invalidate_volume_global(bucket_name: str):
    """Invalidate volume across ALL workers by bumping the Redis generation.

    Call this after any volume config change (versioning, access policy, etc.).
    Every worker will see the generation mismatch on its next get_cached_volume()
    and re-read from the database.
    """
    _bump_redis_generation(bucket_name)
    # Also evict locally for immediate effect in this worker
    _volume_cache.pop(bucket_name, None)


def invalidate_apikey(access_key: str):
    """Call when an API key is modified."""
    _apikey_cache.pop(access_key, None)


def invalidate_acl(volume_pk: str):
    """Call when ACLs for a volume change."""
    to_remove = [k for k in _acl_cache if k[1] == volume_pk]
    for k in to_remove:
        _acl_cache.pop(k, None)


# ─── Metadata cache ─────────────────────────────────────────────────────────

# Reverse index: volume_uuid_hex -> set of (volume_uuid_hex, path) keys in _metadata_cache.
# Allows O(1) per-entry invalidation of all metadata for a volume instead of O(n) scan.
_metadata_by_volume: dict[str, set[tuple[str, str]]] = {}


def get_cached_metadata(volume_uuid_hex: str, path: str) -> Optional[Dict[str, Any]]:
    """Return cached metadata dict if available and not expired."""
    key = (volume_uuid_hex, path)
    entry = _metadata_cache.get(key)
    if entry and entry[1] > time.monotonic():
        _metadata_cache.move_to_end(key)  # mark as recently used
        return entry[0]
    return None


def set_cached_metadata(volume_uuid_hex: str, path: str, attributes: Dict[str, Any]):
    """Cache metadata for a blob. O(1) LRU eviction via OrderedDict."""
    key = (volume_uuid_hex, path)
    if key in _metadata_cache:
        _metadata_cache.move_to_end(key)
    else:
        # Track in reverse index for per-volume invalidation
        _metadata_by_volume.setdefault(volume_uuid_hex, set()).add(key)
    _metadata_cache[key] = (attributes, time.monotonic() + _METADATA_TTL)
    # Evict oldest entry when over capacity — O(1)
    if len(_metadata_cache) > _METADATA_MAX_SIZE:
        evicted_key, _ = _metadata_cache.popitem(last=False)
        vol_set = _metadata_by_volume.get(evicted_key[0])
        if vol_set:
            vol_set.discard(evicted_key)


def invalidate_metadata(volume_uuid_hex: str, path: str):
    """Invalidate cached metadata for a specific blob."""
    key = (volume_uuid_hex, path)
    _metadata_cache.pop(key, None)
    vol_set = _metadata_by_volume.get(volume_uuid_hex)
    if vol_set:
        vol_set.discard(key)


def invalidate_volume_metadata(volume_uuid_hex: str):
    """Invalidate all cached metadata for a volume. O(k) where k = entries for this volume."""
    keys = _metadata_by_volume.pop(volume_uuid_hex, set())
    for key in keys:
        _metadata_cache.pop(key, None)


def clear_all_caches():
    """Clear all caches. Useful after major changes like recreating storage/volumes."""
    _apikey_cache.clear()
    _volume_cache.clear()
    _acl_cache.clear()
    _metadata_cache.clear()
    _metadata_by_volume.clear()
    _volume_perm_cache.clear()


# ─── Volume permission cache ────────────────────────────────────────────────

def get_cached_volume_permission(user_id: int, bucket_name: str, permission: str) -> Optional[bool]:
    """Return cached bucket permission result if available and not expired."""
    entry = _volume_perm_cache.get((user_id, bucket_name, permission))
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def set_cached_volume_permission(user_id: int, bucket_name: str, permission: str, allowed: bool):
    """Cache bucket permission result for a user."""
    _volume_perm_cache[(user_id, bucket_name, permission)] = (allowed, time.monotonic() + _VOLUME_PERM_TTL)
