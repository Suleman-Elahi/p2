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
from typing import AsyncIterator

from p2.s3.volume_pool import BlockCoord, VolumePool

logger = logging.getLogger(__name__)

try:
    from p2.s3.p2_s3_crypto import read_block_uring as rust_read_block_uring, RustUringBlockStreamer
except ImportError:
    rust_read_block_uring = None
    RustUringBlockStreamer = None


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
    """Read *length* bytes from *path* at *offset* using a single pread()."""
    fd = _open_vol_noatime(path)
    try:
        _fadvise_hint(fd, _FADV_RANDOM, offset, length)
        if rust_read_block_uring is not None:
            return rust_read_block_uring(fd, offset, length)
        return os.pread(fd, length, offset)
    finally:
        os.close(fd)


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
    """Read a complete object into memory.  Choose strategy by total size."""
    obj_size = total_size(blocks)
    if obj_size <= _MEDIUM_MAX:
        return await asyncio.to_thread(_read_blocks_full, pool, blocks)
    # For large objects, callers should use stream_blocks instead.
    # This path buffers everything — only call for medium objects.
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
