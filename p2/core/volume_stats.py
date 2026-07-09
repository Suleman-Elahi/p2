"""Helpers for persisted per-volume object and byte counters."""
import json

from django.db.models import F, Value
from django.db.models.functions import Greatest

from p2.core.constants import ATTR_BLOB_IS_FOLDER, ATTR_BLOB_SIZE_BYTES
from p2.core.models import Volume
from p2.s3.engine import get_engine

STATS_INITIALIZED_TAG = "p2.ui.stats_initialized"


import logging
import asyncio
import redis.asyncio as aioredis
from django.conf import settings

logger = logging.getLogger(__name__)

_REDIS_CLIENT = None
_DIRTY_VOLUMES = set()
_FLUSH_LOOP_TASK = None

def _get_redis() -> aioredis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
        )
    return _REDIS_CLIENT


async def _flush_all_dirty():
    if not _DIRTY_VOLUMES:
        return
    vols_to_flush = list(_DIRTY_VOLUMES)
    _DIRTY_VOLUMES.clear()

    for vol_uuid in vols_to_flush:
        try:
            r = _get_redis()
            key = f"p2:volume:{vol_uuid}:stats"
            stats = await r.hgetall(key)
            if not stats:
                continue

            obj_count = max(0, int(stats.get("object_count", 0)))
            bytes_used = max(0, int(stats.get("space_used_bytes", 0)))

            from asgiref.sync import sync_to_async
            
            def _update_db(uuid_val, count, bytes_val):
                from p2.core.models import Volume
                Volume.objects.filter(uuid=uuid_val).update(
                    object_count=count,
                    space_used_bytes=bytes_val,
                )
                
            await sync_to_async(_update_db, thread_sensitive=False)(vol_uuid, obj_count, bytes_used)
        except Exception as exc:
            logger.error("Failed to flush volume %s stats to database: %s", vol_uuid, exc)
            # Re-add on error only if we're not shutting down
            _DIRTY_VOLUMES.add(vol_uuid)


async def _flush_loop():
    """Background task to periodically flush dirty volume stats to the SQLite database."""
    try:
        while True:
            await asyncio.sleep(2.0)  # Flush every 2 seconds
            if not _DIRTY_VOLUMES:
                break
            await _flush_all_dirty()
    except asyncio.CancelledError:
        # Flush one last time on cancellation/shutdown
        await _flush_all_dirty()
        raise


async def adjust_volume_stats(volume, object_delta=0, bytes_delta=0):
    """Atomically adjust persisted counters for a volume using Redis and background SQLite flushing."""
    if not volume.tags.get(STATS_INITIALIZED_TAG):
        volume.tags[STATS_INITIALIZED_TAG] = True
        await volume.asave(update_fields=["tags"])

    vol_uuid = volume.uuid.hex
    success = False
    try:
        r = _get_redis()
        key = f"p2:volume:{vol_uuid}:stats"
        
        # Initialize hash if it doesn't exist
        exists = await r.exists(key)
        if not exists:
            await r.hset(key, mapping={
                "object_count": str(volume.object_count),
                "space_used_bytes": str(volume.space_used_bytes)
            })
            await r.expire(key, 86400 * 7)  # expire in 7 days
            
        await r.hincrby(key, "object_count", object_delta)
        await r.hincrby(key, "space_used_bytes", bytes_delta)
        
        _DIRTY_VOLUMES.add(vol_uuid)
        success = True
    except Exception as exc:
        logger.warning("Failed to update volume stats in Redis: %s. Falling back to direct database update.", exc)

    if not success:
        # Direct fallback path (direct SQLite write)
        await Volume.objects.filter(pk=volume.pk).aupdate(
            object_count=Greatest(Value(0), F("object_count") + Value(object_delta)),
            space_used_bytes=Greatest(Value(0), F("space_used_bytes") + Value(bytes_delta)),
        )
        return

    # Trigger background loop if not already running
    global _FLUSH_LOOP_TASK
    if _FLUSH_LOOP_TASK is None or _FLUSH_LOOP_TASK.done():
        _FLUSH_LOOP_TASK = asyncio.create_task(_flush_loop())


def adjust_volume_stats_sync(volume, object_delta=0, bytes_delta=0):
    """Sync wrapper for request paths that are still synchronous."""
    Volume.objects.filter(pk=volume.pk).update(
        object_count=Greatest(Value(0), F("object_count") + Value(object_delta)),
        space_used_bytes=Greatest(Value(0), F("space_used_bytes") + Value(bytes_delta)),
    )

    if not volume.tags.get(STATS_INITIALIZED_TAG):
        volume.tags[STATS_INITIALIZED_TAG] = True
        volume.save(update_fields=["tags"])


def scan_volume_stats(volume):
    """Scan LMDB metadata once to derive counters for a volume.

    Handles both the new block-based schema (``size`` key) and the legacy
    ``internal_path`` schema (``blob.p2.io/size`` key).
    """
    engine = get_engine(volume)
    object_count = 0
    total_bytes = 0

    for key, metadata_json in engine.list('', None, None):
        if key.startswith('/.'):  # skip internal multipart keys
            continue
        try:
            attributes = json.loads(metadata_json)
        except (TypeError, ValueError):
            continue

        # Skip folders (legacy schema) and delete markers
        if attributes.get(ATTR_BLOB_IS_FOLDER, attributes.get('is_folder', False)):
            continue

        object_count += 1
        # New schema uses ``size``; legacy schema uses ATTR_BLOB_SIZE_BYTES
        size = int(
            attributes.get('size', 0)
            or attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
            or 0
        )
        total_bytes += size

    return object_count, total_bytes


def recalculate_volume_stats(volume):
    """Recompute and persist counters for a volume."""
    object_count, total_bytes = scan_volume_stats(volume)
    volume.object_count = object_count
    volume.space_used_bytes = total_bytes
    volume.tags[STATS_INITIALIZED_TAG] = True
    volume.save(update_fields=["object_count", "space_used_bytes", "tags"])
    return object_count, total_bytes
