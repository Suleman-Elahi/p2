"""Zero-copy block reader for volume files.

Reads object data by executing ``pread(2)`` (or ``os.sendfile``) calls directly
on the preallocated ``.bin`` volume files at the exact ``(offset, length)``
coordinates stored in LMDB metadata.

No temporary file copies are ever created. Multi-block objects (multipart
uploads) are streamed transparently by iterating over their block list.

Serving modes
-------------
* **Small objects** (≤ 64 KiB): ``pread`` into a bytes buffer — single syscall.
* **Medium objects** (64 KiB – 4 MiB): ``pread`` with ``posix_fadvise(RANDOM)``.
* **Large objects** (> 4 MiB): async generator yielding 4 MiB chunks via
  ``pread`` with ``posix_fadvise(SEQUENTIAL)``.
* **Range requests**: same as above but slices the block list to honour the
  requested byte range.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import AsyncIterator

from p2.s3.volume_pool import BlockCoord, VolumePool

logger = logging.getLogger(__name__)

try:
    from p2.s3.p2_s3_crypto import read_block_uring as rust_read_block_uring, RustUringBlockStreamer
except ImportError:
    rust_read_block_uring = None
    RustUringBlockStreamer = None


# ---------------------------------------------------------------------------
# FD cache — avoid open+close per pread (saves 2 syscalls per block read)
# ---------------------------------------------------------------------------

class _FDCache:
    """Thread-safe LRU cache of open file descriptors for volume files.

    Each entry is (fd, last_used_mono). Evicts entries idle for >30s.
    Max 64 entries (covers all active + recently sealed volumes).
    """
    def __init__(self, max_size: int = 64, idle_timeout: float = 30.0):
        self._max_size = max_size
        self._idle_timeout = idle_timeout
        self._lock = threading.Lock()
        # path -> (fd, last_used)
        self._cache: dict[str, tuple[int, float]] = {}

    def get(self, path: str) -> tuple[int, bool]:
        """Return (fd, is_hit) for *path*, reusing a cached one or opening fresh.

        The is_hit flag tells the caller to skip fadvise (kernel already
        has the pages hot in page cache for recently-used FDs).
        """
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(path)
            if entry is not None:
                fd, _ = entry
                self._cache[path] = (fd, now)
                return fd, True

        # Open outside the lock to avoid holding it during I/O
        fd = _open_vol_noatime(path)
        with self._lock:
            # Evict oldest if over capacity
            if len(self._cache) >= self._max_size:
                self._evict_expired(now)
                if len(self._cache) >= self._max_size:
                    self._evict_oldest()
            self._cache[path] = (fd, now)
        return fd, False

    def close_all(self) -> None:
        """Close all cached FDs (call on shutdown)."""
        with self._lock:
            for fd, _ in self._cache.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._cache.clear()

    def _evict_expired(self, now: float) -> None:
        expired = [p for p, (_, ts) in self._cache.items() if now - ts > self._idle_timeout]
        for p in expired:
            fd, _ = self._cache.pop(p)
            try:
                os.close(fd)
            except OSError:
                pass

    def _evict_oldest(self) -> None:
        if not self._cache:
            return
        oldest_path = min(self._cache, key=lambda p: self._cache[p][1])
        fd, _ = self._cache.pop(oldest_path)
        try:
            os.close(fd)
        except OSError:
            pass

_fd_cache = _FDCache()


# Read-ahead thresholds (bytes)
_SMALL_MAX = 64 * 1024         # 64 KiB  — single pread, no fadvise overhead
_MEDIUM_MAX = 4 * 1024 * 1024  # 4 MiB   — buffered pread with fadvise(RANDOM)
_STREAM_CHUNK = 4 * 1024 * 1024  # 4 MiB stream chunks

# posix_fadvise advice constants (Linux)
_FADV_RANDOM = 1
_FADV_SEQUENTIAL = 2

try:
    import ctypes
    import ctypes.util
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _fadvise = _libc.posix_fadvise
    _fadvise.restype = ctypes.c_int
    _fadvise.argtypes = [ctypes.c_int, ctypes.c_long, ctypes.c_long, ctypes.c_int]
except Exception:
    _fadvise = None


def _fadvise_hint(fd: int, advice: int, offset: int = 0, length: int = 0) -> None:
    if _fadvise is not None:
        try:
            _fadvise(fd, offset, length, advice)
        except Exception:
            pass


def _open_vol_noatime(path: str) -> int:
    """Open a volume file read-only with O_NOATIME when available."""
    flags = os.O_RDONLY
    try:
        flags |= os.O_NOATIME
    except AttributeError:
        pass
    try:
        return os.open(path, flags)
    except OSError:
        return os.open(path, os.O_RDONLY)


# ---------------------------------------------------------------------------
# Block list helpers
# ---------------------------------------------------------------------------

def total_size(blocks: list[BlockCoord]) -> int:
    """Sum of all block lengths — the logical object size."""
    return sum(b.length for b in blocks)


def slice_blocks(blocks: list[BlockCoord], range_start: int, range_end: int) -> list[tuple[BlockCoord, int, int]]:
    """Slice a block list to satisfy a byte range [range_start, range_end] inclusive.

    Returns a list of ``(block, read_offset, read_length)`` tuples where
    ``read_offset`` is relative to the start of *block*.
    """
    result: list[tuple[BlockCoord, int, int]] = []
    cursor = 0
    for block in blocks:
        block_end = cursor + block.length - 1
        if block_end < range_start:
            cursor += block.length
            continue
        if cursor > range_end:
            break
        # Overlap: clamp to requested range
        local_start = max(0, range_start - cursor)
        local_end = min(block.length - 1, range_end - cursor)
        result.append((block, local_start, local_end - local_start + 1))
        cursor += block.length
    return result


# ---------------------------------------------------------------------------
# Synchronous reads (run via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _pread_block(path: str, offset: int, length: int) -> bytes:
    """Read *length* bytes from *path* at *offset* using a single pread().

    Uses the FD cache to avoid open+close per call (saves 2 syscalls).
    Skips fadvise on cache hits (pages already hot in kernel page cache).

    NOTE: This path deliberately uses ``os.pread`` and never the Rust
    ``read_block_uring``. For the small/medium objects that reach here
    (≤ 4 MiB), the uring implementation funnels every read through a single
    global uring thread via a crossbeam channel + oneshot round-trip and
    ``dup(2)`` per call. For a page-cache hit (~1-2us) that hand-off costs
    more than the pread it replaces and serializes all small reads in the
    worker. Plain pread is both faster and fully parallel here.
    """
    fd, is_hit = _fd_cache.get(path)
    if not is_hit:
        _fadvise_hint(fd, _FADV_RANDOM, offset, length)
    return os.pread(fd, length, offset)


def _read_blocks_full(pool: VolumePool, blocks: list[BlockCoord]) -> bytes:
    """Read all blocks and concatenate — used for small/medium objects."""
    parts: list[bytes] = []
    for block in blocks:
        path = pool.get_volume_path(block.vol_uuid)
        parts.append(_pread_block(path, block.offset, block.length))
    return b"".join(parts)


def _read_sliced_blocks(pool: VolumePool, slices: list[tuple[BlockCoord, int, int]]) -> bytes:
    """Read sliced block ranges and concatenate — used for range requests."""
    parts: list[bytes] = []
    for block, local_off, length in slices:
        path = pool.get_volume_path(block.vol_uuid)
        parts.append(_pread_block(path, block.offset + local_off, length))
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Streaming reads (async generator, used for large objects)
# ---------------------------------------------------------------------------

async def stream_blocks(
    pool: VolumePool,
    blocks: list[BlockCoord],
    chunk_size: int = _STREAM_CHUNK,
) -> AsyncIterator[bytes]:
    """Async generator that yields chunks from a block list.

    Uses ``pread`` via ``asyncio.to_thread`` so the event loop is never blocked.
    Applies ``posix_fadvise(SEQUENTIAL)`` per volume for kernel readahead.
    """
    if RustUringBlockStreamer is not None:
        raw_blocks = [b.to_dict() for b in blocks]
        streamer = RustUringBlockStreamer(raw_blocks, pool._vol_dir, chunk_size)
        while True:
            chunk = await asyncio.to_thread(streamer.next_chunk)
            if chunk is None:
                break
            yield chunk
        return

    for block in blocks:
        path = pool.get_volume_path(block.vol_uuid)
        remaining = block.length
        pos = block.offset

        # Open FD once per block and apply SEQUENTIAL hint
        fd = await asyncio.to_thread(_open_vol_noatime, path)
        try:
            _fadvise_hint(fd, _FADV_SEQUENTIAL, block.offset, block.length)
            while remaining > 0:
                to_read = min(chunk_size, remaining)
                chunk = await asyncio.to_thread(os.pread, fd, to_read, pos)
                if not chunk:
                    break
                yield chunk
                pos += len(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)


async def stream_sliced_blocks(
    pool: VolumePool,
    slices: list[tuple[BlockCoord, int, int]],
    chunk_size: int = _STREAM_CHUNK,
) -> AsyncIterator[bytes]:
    """Async generator for range-request sliced blocks."""
    if RustUringBlockStreamer is not None:
        sliced_blocks = []
        for block, local_off, length in slices:
            sliced_blocks.append({
                "vol_uuid": block.vol_uuid,
                "offset": block.offset + local_off,
                "length": length
            })
        streamer = RustUringBlockStreamer(sliced_blocks, pool._vol_dir, chunk_size)
        while True:
            chunk = await asyncio.to_thread(streamer.next_chunk)
            if chunk is None:
                break
            yield chunk
        return

    for block, local_off, length in slices:
        path = pool.get_volume_path(block.vol_uuid)
        remaining = length
        pos = block.offset + local_off

        fd = await asyncio.to_thread(_open_vol_noatime, path)
        try:
            _fadvise_hint(fd, _FADV_RANDOM, pos, length)
            while remaining > 0:
                to_read = min(chunk_size, remaining)
                chunk = await asyncio.to_thread(os.pread, fd, to_read, pos)
                if not chunk:
                    break
                yield chunk
                pos += len(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# High-level entry points called from views
# ---------------------------------------------------------------------------

async def read_object(pool: VolumePool, blocks: list[BlockCoord]) -> bytes:
    """Read a complete object into memory.
    
    CRITICAL OPTIMIZATION: Small objects (≤ 64 KiB) are read inline with pread
    directly in the event loop. The syscall completes in ~1-5us for page-cache
    hits, avoiding the ~30-100us asyncio.to_thread dispatch overhead.
    
    This is safe because:
    1. pread is a single non-blocking syscall
    2. FD cache eliminates open/close overhead
    3. No thread hop = 10x faster for small objects
    
    Medium/large objects still use thread pool to avoid blocking the loop.
    """
    obj_size = total_size(blocks)
    if obj_size <= _SMALL_MAX:
        # INLINE READ - no thread hop!
        return _read_blocks_full(pool, blocks)
    # Large objects: thread pool
    return await asyncio.to_thread(_read_blocks_full, pool, blocks)


async def read_range(
    pool: VolumePool,
    blocks: list[BlockCoord],
    range_start: int,
    range_end: int,
) -> bytes:
    """Read a byte range [range_start, range_end] inclusive from a block list."""
    slices = slice_blocks(blocks, range_start, range_end)
    return await asyncio.to_thread(_read_sliced_blocks, pool, slices)
