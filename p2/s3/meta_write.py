"""Bounded async queue for LMDB metadata writes with batching.

Architecture
------------
Each Granian worker process runs exactly one ``_write_worker`` asyncio Task
backed by a dedicated thread for LMDB commits (avoids shared threadpool
contention that adds 2-3ms of dispatch latency under concurrency).

When a PUT request calls ``write_metadata``:

1. A ``asyncio.Future`` (ack) is created and the item is pushed onto the
   in-process ``asyncio.Queue``.
2. The PUT coroutine ``await``s the Future — it is *suspended* (non-blocking)
   until the worker commits.
3. The write worker drains as many items as available (up to BATCH_SIZE) into
   a *single* LMDB write transaction, then resolves every Future at once.

This collapses N concurrent PUTs into ~1 LMDB commit instead of N, cutting
B-tree and lock overhead proportionally while still guaranteeing that the HTTP
200 is only sent after the metadata is safely in LMDB.

Fallback
--------
If the queue is disabled (``S3_METADATA_WRITE_QUEUE_ENABLED=false``), or if
the queue is full, the write falls back to a direct ``asyncio.to_thread`` call
— one LMDB transaction per PUT, no batching.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

logger = logging.getLogger(__name__)

# Per-event-loop state (each Granian worker process has its own event loop).
_WRITE_QUEUE: asyncio.Queue | None = None
_WRITE_WORKER_TASK: asyncio.Task | None = None
_WRITE_INIT_LOCK: asyncio.Lock | None = None
# Dedicated single-thread executor for LMDB commits — avoids contention
# with the shared asyncio threadpool used by to_thread().
_LMDB_EXECUTOR: ThreadPoolExecutor | None = None


# ---------------------------------------------------------------------------
# Settings helpers (read once per call; values are module-level Django attrs)
# ---------------------------------------------------------------------------

def _queue_enabled() -> bool:
    return bool(getattr(settings, "S3_METADATA_WRITE_QUEUE_ENABLED", False))


def _queue_max_size() -> int:
    return max(1, int(getattr(settings, "S3_METADATA_WRITE_QUEUE_MAX_SIZE", 8192)))


def _batch_size() -> int:
    return max(1, int(getattr(settings, "S3_METADATA_WRITE_BATCH_SIZE", 64)))


def _batch_window_ms() -> float:
    """Max milliseconds to wait for more items before flushing an incomplete batch."""
    return max(0.0, float(getattr(settings, "S3_METADATA_WRITE_BATCH_WINDOW_MS", 5.0)))


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

async def _ensure_write_worker() -> None:
    """Start the batching write worker task if not already running."""
    global _WRITE_QUEUE, _WRITE_WORKER_TASK, _WRITE_INIT_LOCK, _LMDB_EXECUTOR

    # Fast path — worker already running.
    if _WRITE_WORKER_TASK is not None and not _WRITE_WORKER_TASK.done():
        return

    # Lazy-init the lock itself (cannot create at import time — no event loop).
    if _WRITE_INIT_LOCK is None:
        _WRITE_INIT_LOCK = asyncio.Lock()

    async with _WRITE_INIT_LOCK:
        if _WRITE_QUEUE is None:
            _WRITE_QUEUE = asyncio.Queue(maxsize=_queue_max_size())
        if _LMDB_EXECUTOR is None:
            _LMDB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lmdb-writer")
        if _WRITE_WORKER_TASK is None or _WRITE_WORKER_TASK.done():
            if _WRITE_WORKER_TASK is not None and _WRITE_WORKER_TASK.done():
                exc = _WRITE_WORKER_TASK.exception()
                if exc:
                    logger.error("metadata write worker crashed, restarting: %s", exc)
            _WRITE_WORKER_TASK = asyncio.create_task(
                _write_worker(), name="p2-metadata-write-worker"
            )


# ---------------------------------------------------------------------------
# Batching write worker
# ---------------------------------------------------------------------------

async def _write_worker() -> None:
    """Drain the queue in batches and commit each batch in a single LMDB txn."""
    assert _WRITE_QUEUE is not None
    max_batch = _batch_size()
    window_s = _batch_window_ms() / 1000.0

    try:
        while True:
            # Block until at least one item arrives.
            batch: list[tuple] = []
            try:
                first = await _WRITE_QUEUE.get()
                batch.append(first)
            except asyncio.CancelledError:
                break

            # Drain everything already queued without waiting — this is the key
            # optimization. Under concurrency, multiple PUTs queue items while
            # we're in to_thread() doing the previous commit. Draining them all
            # immediately means we batch N items into 1 fsync instead of paying
            # the window delay per batch.
            while len(batch) < max_batch:
                try:
                    batch.append(_WRITE_QUEUE.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # If we only got 1 item and the window is positive, wait briefly
            # for stragglers — but only when there's nothing queued yet.
            if len(batch) == 1 and window_s > 0:
                deadline = asyncio.get_event_loop().time() + window_s
                while len(batch) < max_batch:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(
                            _WRITE_QUEUE.get(),
                            timeout=remaining,
                        )
                        batch.append(item)
                        # Once we got a second item, drain the rest immediately
                        while len(batch) < max_batch:
                            try:
                                batch.append(_WRITE_QUEUE.get_nowait())
                            except asyncio.QueueEmpty:
                                break
                        break
                    except asyncio.TimeoutError:
                        break

            await _flush_batch(batch)
    except asyncio.CancelledError:
        pass
    finally:
        # Drain any remaining items in the queue so no writes are lost.
        remaining_batch: list[tuple] = []
        while not _WRITE_QUEUE.empty():
            try:
                remaining_batch.append(_WRITE_QUEUE.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining_batch:
            logger.info("metadata write worker draining %d remaining items on shutdown", len(remaining_batch))
            await _flush_batch(remaining_batch)


async def _flush_batch(batch: list[tuple]) -> None:
    """Write all items in *batch* using the minimum number of LMDB transactions.

    Items are ``(engine, path, metadata_json, future_or_None)``.
    Groups items by engine instance so each LMDB environment gets exactly one
    write transaction per flush.
    """
    # Group by engine identity (object id is stable within a process).
    groups: dict[int, tuple] = {}  # engine_id -> (engine, [(path, json, fut)])
    for engine, path, metadata_json, fut in batch:
        eid = id(engine)
        if eid not in groups:
            groups[eid] = (engine, [])
        groups[eid][1].append((path, metadata_json, fut))

    for engine, items in groups.values():
        resolved: list[tuple] = []
        try:
            # Run the entire group as a single synchronous LMDB transaction
            # on the dedicated LMDB thread — avoids shared threadpool contention.
            def _commit(items=items, engine=engine):
                with engine.env.begin(write=True, db=engine.db) as txn:
                    for path, metadata_json, _ in items:
                        txn.put(
                            path.encode("utf-8"),
                            metadata_json.encode("utf-8"),
                        )

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(_LMDB_EXECUTOR, _commit)
            resolved = [(path, metadata_json, fut, None) for path, metadata_json, fut in items]
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "batched metadata write failed (%d items): %s", len(items), exc
            )
            resolved = [(path, metadata_json, fut, exc) for path, metadata_json, fut in items]

        # Resolve all futures outside the thread — event loop is required.
        for _path, _json, fut, exc in resolved:
            if fut is None or fut.done():
                continue
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def write_metadata(engine, path: str, metadata_json: str) -> bool:
    """Write object metadata to LMDB, blocking until committed.

    When the queue is enabled:
        * The item is pushed onto the per-worker ``asyncio.Queue``.
        * The caller suspends (non-blocking) until the batching worker commits
          the transaction and resolves the ``Future``.
        * Multiple concurrent PUTs are coalesced into a single LMDB commit.

    When the queue is disabled or full (fallback):
        * ``asyncio.to_thread`` runs a single LMDB write transaction directly.
        * One commit per PUT — no batching, but always correct.

    Returns ``True`` on success, raises on failure.
    """
    if _queue_enabled():
        await _ensure_write_worker()
        fut = asyncio.get_running_loop().create_future()
        try:
            _WRITE_QUEUE.put_nowait((engine, path, metadata_json, fut))  # type: ignore[union-attr]
            await fut
            return True
        except asyncio.QueueFull:
            logger.warning(
                "metadata write queue full; falling back to direct write for %s", path
            )
            # Fall through to the direct path below.

    # Direct path: one LMDB transaction, no batching.
    await asyncio.to_thread(engine.put, path, metadata_json)
    return True
