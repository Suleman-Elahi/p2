"""Group Committer — Batched async writes to volume files + LMDB metadata.

Architecture
------------
Each Granian worker process runs exactly one ``_commit_worker`` asyncio Task.

When a PUT request calls ``write_block``:

1. The payload bytes, pre-computed hashes, and a ``asyncio.Future`` (ack) are
   pushed onto the in-process ``asyncio.Queue``.
2. The PUT coroutine ``await``s the Future — it is *suspended* (non-blocking)
   until the group commit is done.
3. The commit worker drains up to ``BATCH_SIZE`` items, calls the Rust engine's
   ``pwrite`` (or Python ``os.pwrite``) for each item's payload, then commits
   ALL metadata entries in one LMDB write transaction and resolves every Future.

This collapses N concurrent PUTs into ≈1 ``fdatasync`` + 1 LMDB commit
instead of N of each, cutting I/O overhead proportionally while still
guaranteeing that the HTTP 200 is only sent after data is safely on disk.

Fallback
--------
If the queue is full, the write falls back to a direct synchronous path.
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from p2.s3.volume_pool import VolumeHandle

logger = logging.getLogger(__name__)

try:
    from p2.s3.p2_s3_crypto import GroupCommitter as RustGroupCommitter
except ImportError:
    RustGroupCommitter = None

_RUST_COMMITTER: RustGroupCommitter | None = None


# ---------------------------------------------------------------------------
# Per-event-loop state (each Granian worker has its own event loop)
# ---------------------------------------------------------------------------

_COMMIT_QUEUE: asyncio.Queue | None = None
_COMMIT_WORKER_TASK: asyncio.Task | None = None
_COMMIT_INIT_LOCK: asyncio.Lock | None = None
# Dedicated single-thread executor for I/O + LMDB commits — avoids contention
# with the shared asyncio threadpool.
_IO_EXECUTOR: ThreadPoolExecutor | None = None

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _queue_enabled() -> bool:
    return bool(getattr(settings, "S3_METADATA_WRITE_QUEUE_ENABLED", True))


def _queue_max_size() -> int:
    return max(1, int(getattr(settings, "S3_METADATA_WRITE_QUEUE_MAX_SIZE", 8192)))


def _batch_size() -> int:
    return max(1, int(getattr(settings, "S3_METADATA_WRITE_BATCH_SIZE", 64)))


def _batch_window_ms() -> float:
    """Max milliseconds to wait for stragglers before flushing an incomplete batch."""
    return max(0.0, float(getattr(settings, "S3_METADATA_WRITE_BATCH_WINDOW_MS", 4.0)))


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

async def _ensure_commit_worker() -> None:
    """Start the group-commit worker task if not already running."""
    global _COMMIT_QUEUE, _COMMIT_WORKER_TASK, _COMMIT_INIT_LOCK, _IO_EXECUTOR, _RUST_COMMITTER

    current_loop = asyncio.get_running_loop()

    if RustGroupCommitter is not None and _RUST_COMMITTER is None:
        _RUST_COMMITTER = RustGroupCommitter(_batch_size(), int(_batch_window_ms()))

    if _COMMIT_WORKER_TASK is not None and not _COMMIT_WORKER_TASK.done():
        try:
            # Check loop binding to avoid Task from a previous closed event loop
            if _COMMIT_WORKER_TASK.get_loop() == current_loop:
                return
        except AttributeError:
            pass

    if _COMMIT_INIT_LOCK is None or getattr(_COMMIT_INIT_LOCK, "_loop", None) != current_loop:
        _COMMIT_INIT_LOCK = asyncio.Lock()

    async with _COMMIT_INIT_LOCK:
        if _COMMIT_QUEUE is None or getattr(_COMMIT_QUEUE, "_loop", None) != current_loop:
            _COMMIT_QUEUE = asyncio.Queue(maxsize=_queue_max_size())
        if _IO_EXECUTOR is None:
            _IO_EXECUTOR = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="p2-vol-writer",
            )
        if _COMMIT_WORKER_TASK is None or _COMMIT_WORKER_TASK.done():
            if _COMMIT_WORKER_TASK is not None and _COMMIT_WORKER_TASK.done():
                exc = _COMMIT_WORKER_TASK.exception()
                if exc:
                    logger.error("group-commit worker crashed, restarting: %s", exc)
            _COMMIT_WORKER_TASK = asyncio.create_task(
                _commit_worker(), name="p2-group-commit-worker"
            )


# ---------------------------------------------------------------------------
# Group Commit Worker
# ---------------------------------------------------------------------------

async def _commit_worker() -> None:
    """Drain the queue in batches, write data, then commit metadata in bulk."""
    assert _COMMIT_QUEUE is not None
    max_batch = _batch_size()
    window_s = _batch_window_ms() / 1000.0

    try:
        while True:
            batch: list[tuple] = []
            try:
                first = await _COMMIT_QUEUE.get()
                batch.append(first)
            except asyncio.CancelledError:
                break

            # Drain everything already queued immediately — key optimization
            while len(batch) < max_batch:
                try:
                    batch.append(_COMMIT_QUEUE.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # Wait briefly for stragglers if batch is small and window is set
            if len(batch) == 1 and window_s > 0:
                deadline = asyncio.get_event_loop().time() + window_s
                while len(batch) < max_batch:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(
                            _COMMIT_QUEUE.get(), timeout=remaining
                        )
                        batch.append(item)
                        # Got a second item — drain immediately
                        while len(batch) < max_batch:
                            try:
                                batch.append(_COMMIT_QUEUE.get_nowait())
                            except asyncio.QueueEmpty:
                                break
                        break
                    except asyncio.TimeoutError:
                        break

            await _flush_batch(batch)
    except asyncio.CancelledError:
        pass
    finally:
        # Drain remaining items on shutdown so no writes are lost
        remaining_batch: list[tuple] = []
        assert _COMMIT_QUEUE is not None
        while not _COMMIT_QUEUE.empty():
            try:
                remaining_batch.append(_COMMIT_QUEUE.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining_batch:
            logger.info(
                "group-commit worker draining %d remaining items on shutdown",
                len(remaining_batch),
            )
            await _flush_batch(remaining_batch)


async def _flush_batch(batch: list[tuple]) -> None:
    """Write all batch items to disk and commit their metadata to LMDB.

    Each item is a tuple:
        (handle, offset, data_bytes, engine, lmdb_key, metadata_json, future_or_None)

    Items sharing the same volume file are written in one ``pwrite`` loop.
    All LMDB metadata updates go in a single write transaction per engine.
    """
    assert _IO_EXECUTOR is not None

    def _do_flush(batch=batch) -> list[tuple]:
        """Runs on the dedicated IO thread."""
        # Group by volume handle for bulk pwrite
        vol_groups: dict[str, list[tuple]] = {}
        for item in batch:
            handle, offset, data, engine, lmdb_key, meta_json, fut = item
            vol_groups.setdefault(handle.uuid_hex, []).append(item)

        errors: dict[int, Exception] = {}  # id(item) -> exc

        for uid, items in vol_groups.items():
            handle = items[0][0]
            try:
                fd = handle.fd
                for item in items:
                    _, off, data, *_ = item
                    if data:
                        written = os.pwrite(fd, data, off)
                        if written != len(data):
                            raise OSError(
                                f"pwrite partial: wrote {written}/{len(data)} bytes"
                            )
                # fdatasync once per volume per batch — single syscall for all writes
                os.fdatasync(fd)
            except Exception as exc:
                for item in items:
                    errors[id(item)] = exc

        # Group by LMDB engine for bulk metadata commit
        engine_groups: dict[int, tuple] = {}
        for item in batch:
            _, _, _, engine, lmdb_key, meta_json, _ = item
            eid = id(engine)
            if eid not in engine_groups:
                engine_groups[eid] = (engine, [])
            engine_groups[eid][1].append(item)

        for engine, items in engine_groups.values():
            group_error = None
            try:
                with engine.env.begin(write=True, db=engine.db) as txn:
                    for item in items:
                        if id(item) in errors:
                            continue
                        _, _, _, _, lmdb_key, meta_json, _ = item
                        txn.put(
                            lmdb_key.encode("utf-8"),
                            meta_json.encode("utf-8"),
                        )
            except Exception as exc:
                group_error = exc
                for item in items:
                    if id(item) not in errors:
                        errors[id(item)] = exc

        # Return resolution results: (item, exc_or_None)
        return [(item, errors.get(id(item))) for item in batch]

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(_IO_EXECUTOR, _do_flush)

    for item, exc in results:
        fut = item[6]
        if fut is None or fut.done():
            continue
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _commit_metadata_direct(engine, lmdb_key: str, metadata_json: str) -> None:
    with engine.env.begin(write=True, db=engine.db) as txn:
        txn.put(lmdb_key.encode("utf-8"), metadata_json.encode("utf-8"))


async def write_block(
    handle: "VolumeHandle",
    offset: int,
    data: bytes,
    engine,
    lmdb_key: str,
    metadata_json: str,
) -> bool:
    """Write *data* to *handle* at *offset* and commit *metadata_json* to LMDB.

    When the queue is enabled:
      - Suspends the caller (non-blocking) until the batch commit resolves.
      - Multiple concurrent callers are coalesced into one fdatasync + one LMDB txn.

    Falls back to direct I/O + LMDB when queue is full or disabled.

    Returns True on success, raises on failure.
    """
    if _queue_enabled():
        await _ensure_commit_worker()
        if _RUST_COMMITTER is not None:
            try:
                # Submit via Rust GroupCommitter (GIL-free)
                await asyncio.to_thread(
                    _RUST_COMMITTER.submit,
                    handle.fd,
                    offset,
                    data,
                )
                await asyncio.to_thread(_commit_metadata_direct, engine, lmdb_key, metadata_json)
                return True
            except Exception as exc:
                logger.warning("Rust group-commit failed: %s; falling back to Python queue", exc)

        fut = asyncio.get_running_loop().create_future()
        try:
            assert _COMMIT_QUEUE is not None
            _COMMIT_QUEUE.put_nowait((handle, offset, data, engine, lmdb_key, metadata_json, fut))
            await fut
            return True
        except asyncio.QueueFull:
            logger.warning(
                "group-commit queue full; falling back to direct write for key=%s", lmdb_key
            )

    # Direct path — one fdatasync + one LMDB txn, no batching
    await asyncio.to_thread(_direct_write, handle, offset, data, engine, lmdb_key, metadata_json)
    return True


def _direct_write(
    handle: "VolumeHandle",
    offset: int,
    data: bytes,
    engine,
    lmdb_key: str,
    metadata_json: str,
) -> None:
    """Synchronous direct write — used as fallback path."""
    if data:
        fd = handle.fd
        written = os.pwrite(fd, data, offset)
        if written != len(data):
            raise OSError(f"pwrite partial: wrote {written}/{len(data)} bytes")
        os.fdatasync(fd)
    with engine.env.begin(write=True, db=engine.db) as txn:
        txn.put(lmdb_key.encode("utf-8"), metadata_json.encode("utf-8"))

