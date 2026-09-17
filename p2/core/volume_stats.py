"""Helpers for persisted per-volume object and byte counters."""
from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import F, Value
from django.db.models.functions import Greatest

from p2.core.constants import ATTR_BLOB_IS_FOLDER, ATTR_BLOB_SIZE_BYTES
from p2.core.models import Volume
from p2.s3.engine import get_engine

STATS_INITIALIZED_TAG = "p2.ui.stats_initialized"
_STATS_BATCH_WINDOW_SECONDS = 0.050

logger = logging.getLogger(__name__)

_REDIS_CLIENT: aioredis.Redis | None = None
_DIRTY_VOLUMES: set[str] = set()
_PENDING_DELTAS: dict[str, tuple[int, int]] = {}
_PENDING_FLUSH_TASK: asyncio.Task[None] | None = None
_FLUSH_LOOP_TASK: asyncio.Task[None] | None = None


def _get_redis() -> aioredis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
        )
    return _REDIS_CLIENT


async def _flush_all_dirty() -> None:
    if not _DIRTY_VOLUMES:
        return
    volumes_to_flush = list(_DIRTY_VOLUMES)
    _DIRTY_VOLUMES.clear()

    for volume_uuid in volumes_to_flush:
        try:
            redis_client = _get_redis()
            key = f"p2:volume:{volume_uuid}:stats"
            stats = await redis_client.hgetall(key)
            if not stats:
                continue

            object_count = max(0, int(stats.get("object_count", 0)))
            bytes_used = max(0, int(stats.get("space_used_bytes", 0)))

            def _update_db(uuid_value: str, count: int, used_bytes: int) -> None:
                Volume.objects.filter(uuid=uuid_value).update(
                    object_count=count,
                    space_used_bytes=used_bytes,
                )

            await sync_to_async(_update_db, thread_sensitive=False)(
                volume_uuid, object_count, bytes_used
            )
        except Exception as exc:
            logger.error("Failed to flush volume %s stats to database: %s", volume_uuid, exc)
            _DIRTY_VOLUMES.add(volume_uuid)


async def _flush_loop() -> None:
    """Persist dirty Redis counters to the database every two seconds."""
    try:
        while True:
            await asyncio.sleep(2.0)
            if not _DIRTY_VOLUMES:
                break
            await _flush_all_dirty()
    except asyncio.CancelledError:
        await _flush_all_dirty()
        raise


def _ensure_persist_loop() -> None:
    global _FLUSH_LOOP_TASK
    if _FLUSH_LOOP_TASK is None or _FLUSH_LOOP_TASK.done():
        _FLUSH_LOOP_TASK = asyncio.create_task(_flush_loop())


async def _flush_pending_deltas() -> None:
    """Coalesce hot-path PUT deltas into one Redis pipeline per worker."""
    global _PENDING_FLUSH_TASK
    try:
        # A short window removes one task and Redis round trip per object while
        # leaving persisted counters near-real-time for normal UI use.
        await asyncio.sleep(_STATS_BATCH_WINDOW_SECONDS)
        pending = dict(_PENDING_DELTAS)
        _PENDING_DELTAS.clear()
        if not pending:
            return

        redis_client = _get_redis()
        pipe = redis_client.pipeline(transaction=False)
        for volume_uuid, (object_delta, bytes_delta) in pending.items():
            key = f"p2:volume:{volume_uuid}:stats"
            pipe.hincrby(key, "object_count", object_delta)
            pipe.hincrby(key, "space_used_bytes", bytes_delta)
            pipe.expire(key, 86400 * 7)
        await pipe.execute()

        _DIRTY_VOLUMES.update(pending)
        _ensure_persist_loop()
    except Exception as exc:
        logger.warning("Failed to batch volume stats in Redis: %s", exc)
        # Preserve deltas for the next update instead of silently losing them.
        for volume_uuid, (object_delta, bytes_delta) in pending.items():
            old_object_delta, old_bytes_delta = _PENDING_DELTAS.get(volume_uuid, (0, 0))
            _PENDING_DELTAS[volume_uuid] = (
                old_object_delta + object_delta,
                old_bytes_delta + bytes_delta,
            )
    finally:
        _PENDING_FLUSH_TASK = None
        if _PENDING_DELTAS:
            _PENDING_FLUSH_TASK = asyncio.create_task(_flush_pending_deltas())


def queue_volume_stats_update(volume: Volume, object_delta: int = 0, bytes_delta: int = 0) -> None:
    """Queue an approximate stats update without per-object network I/O."""
    global _PENDING_FLUSH_TASK

    volume_uuid = volume.uuid.hex
    old_object_delta, old_bytes_delta = _PENDING_DELTAS.get(volume_uuid, (0, 0))
    _PENDING_DELTAS[volume_uuid] = (
        old_object_delta + object_delta,
        old_bytes_delta + bytes_delta,
    )
    if _PENDING_FLUSH_TASK is None or _PENDING_FLUSH_TASK.done():
        _PENDING_FLUSH_TASK = asyncio.create_task(_flush_pending_deltas())


async def adjust_volume_stats(volume: Volume, object_delta: int = 0, bytes_delta: int = 0) -> None:
    """Compatibility wrapper for asynchronous callers."""
    queue_volume_stats_update(volume, object_delta, bytes_delta)


def adjust_volume_stats_sync(volume: Volume, object_delta: int = 0, bytes_delta: int = 0) -> None:
    """Synchronously update counters for legacy synchronous request paths."""
    Volume.objects.filter(pk=volume.pk).update(
        object_count=Greatest(Value(0), F("object_count") + Value(object_delta)),
        space_used_bytes=Greatest(Value(0), F("space_used_bytes") + Value(bytes_delta)),
    )

    if not volume.tags.get(STATS_INITIALIZED_TAG):
        volume.tags[STATS_INITIALIZED_TAG] = True
        volume.save(update_fields=["tags"])


def scan_volume_stats(volume: Volume) -> tuple[int, int]:
    """Scan LMDB metadata once to derive counters for a volume."""
    engine = get_engine(volume)
    object_count = 0
    total_bytes = 0

    for key, metadata_json in engine.list("", None, None):
        if key.startswith("/."):
            continue
        try:
            attributes = json.loads(metadata_json)
        except (TypeError, ValueError):
            continue

        if attributes.get(ATTR_BLOB_IS_FOLDER, attributes.get("is_folder", False)):
            continue

        object_count += 1
        total_bytes += int(
            attributes.get("size", 0)
            or attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
            or 0
        )

    return object_count, total_bytes


def recalculate_volume_stats(volume: Volume) -> tuple[int, int]:
    """Recompute and persist counters for a volume."""
    object_count, total_bytes = scan_volume_stats(volume)
    volume.object_count = object_count
    volume.space_used_bytes = total_bytes
    volume.tags[STATS_INITIALIZED_TAG] = True
    volume.save(update_fields=["object_count", "space_used_bytes", "tags"])
    return object_count, total_bytes
