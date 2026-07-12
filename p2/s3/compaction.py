"""Compaction worker — reclaims space from sealed volume files.

Algorithm (per new_architecture.md §4 Background Compaction)
-------------------------------------------------------------
1. Scan LMDB for all objects across all volumes.
2. Compute, per sealed volume, how many bytes are still "alive" (referenced
   by at least one current object block).
3. If a sealed volume's live-byte ratio drops below ``VOLUME_COMPACT_THRESHOLD``
   (default 30%), migrate its surviving blocks to new active volumes and update
   their LMDB entries.
4. Delete the now-empty sealed volume file to reclaim disk space.

This worker is scheduled as an ARQ cron job.  It is I/O-intensive but runs
entirely in background — no request latency impact.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _compact_threshold() -> float:
    from django.conf import settings
    return float(getattr(settings, "VOLUME_COMPACT_THRESHOLD", 0.30))


async def run_compaction(ctx) -> None:  # noqa: C901
    """ARQ task: scan all volumes, compact those below the live-byte threshold."""
    from django.conf import settings
    from p2.core.models import Volume
    from p2.s3.engine import get_engine
    from p2.s3.volume_pool import BlockCoord, VolumePool

    pool = VolumePool.get()
    threshold = _compact_threshold()
    active_uuids = set(pool.get_active_uuids())
    sealed_uuids = pool.list_sealed_volumes()

    if not sealed_uuids:
        logger.debug("compaction: no sealed volumes found")
        return

    logger.info("compaction: checking %d sealed volumes (threshold=%.0f%%)",
                len(sealed_uuids), threshold * 100)

    # ------------------------------------------------------------------
    # Step 1: scan LMDB across all volumes to build live-block index
    # ------------------------------------------------------------------
    # live_bytes[vol_uuid] = total bytes referenced by current LMDB entries
    live_bytes: dict[str, int] = {uid: 0 for uid in sealed_uuids}
    # live_objects[vol_uuid] = list of (lmdb_key, block_coord) for surviving blocks
    live_objects: dict[str, list[tuple[str, Any, "BlockCoord"]]] = {uid: [] for uid in sealed_uuids}

    async for volume in Volume.objects.all():
        try:
            engine = await asyncio.to_thread(get_engine, volume)
        except Exception as exc:
            logger.warning("compaction: could not open engine for volume %s: %s", volume.uuid.hex, exc)
            continue

        items = await asyncio.to_thread(engine.list, "", None, None, use_cache=False)
        for key, raw in items:
            if key.startswith("/."):
                continue
            try:
                meta = json.loads(raw)
            except (TypeError, ValueError):
                continue
            for block_dict in meta.get("blocks", []):
                uid = block_dict.get("vol_uuid")
                if uid and uid in live_bytes:
                    length = int(block_dict.get("length", 0))
                    live_bytes[uid] += length
                    live_objects[uid].append((key, engine, BlockCoord.from_dict(block_dict)))

    # ------------------------------------------------------------------
    # Step 2: decide which sealed volumes to compact
    # ------------------------------------------------------------------
    vol_size = int(getattr(settings, "VOLUME_SIZE_BYTES", 10 * 1024 * 1024 * 1024))
    for uid in sealed_uuids:
        vol_path = pool.get_volume_path(uid)
        try:
            actual_size = os.path.getsize(vol_path)
        except OSError:
            logger.warning("compaction: volume %s not found on disk, skipping", uid)
            continue

        live = live_bytes.get(uid, 0)
        ratio = live / actual_size if actual_size > 0 else 0.0
        if ratio >= threshold:
            logger.debug("compaction: volume %s is %.1f%% live — skipping", uid, ratio * 100)
            continue

        logger.info(
            "compaction: volume %s is %.1f%% live (%d MiB / %d MiB) — compacting",
            uid, ratio * 100, live // (1024 * 1024), actual_size // (1024 * 1024),
        )

        await _compact_volume(pool, uid, vol_path, live_objects.get(uid, []))

    logger.info("compaction: complete")


async def _compact_volume(
    pool: "VolumePool",
    old_uid: str,
    old_path: str,
    survivors: list[tuple[str, Any, "BlockCoord"]],
) -> None:
    """Migrate surviving blocks from *old_uid* to new active volumes."""
    from p2.s3.volume_pool import BlockCoord

    # Group survivors by (LMDB engine, key) so we can update each object once
    # Some objects may span multiple blocks in the same volume.
    # We need to rewrite the entire metadata entry per object key.

    # Collect all blocks for each (engine, key) pair
    key_blocks: dict[tuple[int, str], tuple[Any, dict]] = {}  # (engine_id, key) -> (engine, {old_block -> new_coord})

    for key, engine, old_block in survivors:
        eid = id(engine)
        if (eid, key) not in key_blocks:
            key_blocks[(eid, key)] = (engine, {})
        key_blocks[(eid, key)][1][id(old_block)] = (old_block, None)

    # Read each object's full metadata and migrate blocks
    for (eid, key), (engine, block_map) in key_blocks.items():
        try:
            raw = await asyncio.to_thread(engine.get, key)
            if not raw:
                continue
            meta = json.loads(raw)
            new_blocks = []
            changed = False
            for b_dict in meta.get("blocks", []):
                b = BlockCoord.from_dict(b_dict)
                if b.vol_uuid == old_uid:
                    # Read the data and write to a new active volume block
                    new_coord = await _migrate_block(pool, old_path, b)
                    new_blocks.append(new_coord.to_dict())
                    changed = True
                else:
                    new_blocks.append(b_dict)
            if changed:
                meta["blocks"] = new_blocks
                new_json = json.dumps(meta)
                await asyncio.to_thread(engine.put, key, new_json)
        except Exception as exc:
            logger.error("compaction: failed to migrate key %s: %s", key, exc)
            return  # Abort this volume's compaction; don't delete it

    # All survivors migrated — delete the old volume file
    try:
        os.chmod(old_path, 0o644)  # make writable before delete
        os.remove(old_path)
        logger.info("compaction: deleted old volume %s, reclaimed %d MiB",
                    old_uid, os.path.getsize(old_path) // (1024 * 1024) if os.path.exists(old_path) else 0)
    except OSError as exc:
        logger.error("compaction: could not delete volume %s: %s", old_uid, exc)


async def _migrate_block(
    pool: "VolumePool",
    src_path: str,
    block: "BlockCoord",
) -> "BlockCoord":
    """Read a block from the old volume and write it into a new active volume."""
    from p2.s3.volume_pool import BlockCoord as BC

    def _read() -> bytes:
        fd = os.open(src_path, os.O_RDONLY)
        try:
            return os.pread(fd, block.length, block.offset)
        finally:
            os.close(fd)

    data = await asyncio.to_thread(_read)

    new_handle, new_offset = await asyncio.to_thread(pool.allocate_block, len(data))

    def _write() -> None:
        os.pwrite(new_handle.fd, data, new_offset)
        os.fdatasync(new_handle.fd)

    await asyncio.to_thread(_write)
    return BC(vol_uuid=new_handle.uuid_hex, offset=new_offset, length=len(data))
