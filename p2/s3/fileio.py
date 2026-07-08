"""Optimized file I/O primitives for S3 object serving.

Best practices for S3-grade blob reads:
- Zero-copy kernel transfer via os.sendfile() (avoids userspace buffer copy)
- posix_fadvise() hints to control OS page cache behavior
- O_NOATIME to avoid metadata updates on read
- mmap for small files (avoids read() syscall overhead)
- Pooled pre-allocated buffers to reduce allocation pressure

Reference: Linux sendfile(2), posix_fadvise(2), mmap(2)
"""
from __future__ import annotations

import ctypes
import ctypes.util
import mmap
import os
import stat
import threading
from typing import Optional

# Load libc for posix_fadvise and sendfile (not in Python stdlib)
_libc_name = ctypes.util.find_library("c")
_libc = ctypes.CDLL(_libc_name, use_errno=True) if _libc_name else None

# posix_fadvise advice constants
POSIX_FADV_NORMAL = 0
POSIX_FADV_RANDOM = 1
POSIX_FADV_SEQUENTIAL = 2
POSIX_FADV_WILLNEED = 3
POSIX_FADV_DONTNEED = 4
POSIX_FADV_NOREUSE = 5

# sendfile prototype: ssize_t sendfile(int out_fd, int in_fd, off_t *offset, off_t count)
# Use c_long for off_t — works on both 32-bit and 64-bit Linux.
_off_t = ctypes.c_long
if _libc:
    _sendfile = _libc.sendfile
    _sendfile.restype = ctypes.c_ssize_t
    _sendfile.argtypes = [
        ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(_off_t), _off_t,
    ]
    _posix_fadvise = _libc.posix_fadvise
    _posix_fadvise.restype = ctypes.c_int
    _posix_fadvise.argtypes = [
        ctypes.c_int, _off_t, _off_t, ctypes.c_int,
    ]
else:
    _sendfile = None
    _posix_fadvise = None


def _get_fd_flags(fd: int) -> int:
    """Get file descriptor flags via fcntl."""
    try:
        import fcntl
        return fcntl.fcntl(fd, fcntl.F_GETFL)
    except (ImportError, OSError):
        return 0


def _set_fd_flags(fd: int, flags: int) -> None:
    """Set file descriptor flags via fcntl."""
    try:
        import fcntl
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except (ImportError, OSError):
        pass


# ---------------------------------------------------------------------------
# posix_fadvise — hint the kernel about access patterns
# ---------------------------------------------------------------------------

def fadvise_random(fd: int) -> None:
    """Advise kernel: random access pattern. Prevents readahead pollution."""
    if _posix_fadvise is None:
        return
    _posix_fadvise(fd, 0, 0, POSIX_FADV_RANDOM)


def fadvise_sequential(fd: int) -> None:
    """Advise kernel: sequential access. Enables readahead."""
    if _posix_fadvise is None:
        return
    _posix_fadvise(fd, 0, 0, POSIX_FADV_SEQUENTIAL)


def fadvise_willneed(fd: int, offset: int, length: int) -> None:
    """Advise kernel: we'll need this range soon. Triggers async prefetch."""
    if _posix_fadvise is None:
        return
    _posix_fadvise(fd, offset, length, POSIX_FADV_WILLNEED)


def fadvise_dontneed(fd: int, offset: int, length: int) -> None:
    """Advise kernel: we won't need this range. Evicts from page cache."""
    if _posix_fadvise is None:
        return
    _posix_fadvise(fd, offset, length, POSIX_FADV_DONTNEED)


# ---------------------------------------------------------------------------
# O_NOATIME — avoid access time updates
# ---------------------------------------------------------------------------

def open_noatime(path: str, mode: str = "rb") -> int:
    """Open file with O_NOATIME to avoid metadata updates on read.

    Falls back to regular open if O_NOATIME is unavailable (non-Linux).
    """
    flags = os.O_RDONLY
    try:
        flags |= os.O_NOATIME
    except AttributeError:
        pass  # Not available on this platform
    try:
        return os.open(path, flags)
    except OSError:
        # Fallback: O_NOATIME may fail if not owner
        return os.open(path, os.O_RDONLY)


# ---------------------------------------------------------------------------
# sendfile — zero-copy kernel transfer
# ---------------------------------------------------------------------------

def sendfile_chunk(
    out_fd: int,
    in_fd: int,
    offset: int,
    count: int,
    chunk_size: int = 1024 * 1024,
) -> int:
    """Transfer data between file descriptors using sendfile().

    Returns total bytes sent. Uses chunked transfers to handle large files
    and avoid blocking the event loop for too long.

    Falls back to read/write loop if sendfile is unavailable.
    """
    if _sendfile is None:
        return _fallback_transfer(out_fd, in_fd, offset, count, chunk_size)

    total_sent = 0
    c_offset = _off_t(offset)

    while total_sent < count:
        to_send = min(chunk_size, count - total_sent)
        sent = _sendfile(out_fd, in_fd, ctypes.byref(c_offset), to_send)
        if sent <= 0:
            if sent == 0:
                break  # EOF
            errno = ctypes.get_errno()
            if errno == 11:  # EAGAIN — would block, try again
                continue
            if errno == 4:  # EINTR — interrupted, retry
                continue
            # Other error — fall back to read/write
            return _fallback_transfer(out_fd, in_fd, offset + total_sent,
                                      count - total_sent, chunk_size)
        total_sent += sent

    return total_sent


def _fallback_transfer(
    out_fd: int,
    in_fd: int,
    offset: int,
    count: int,
    chunk_size: int = 1024 * 1024,
) -> int:
    """Fallback read/write transfer when sendfile is unavailable."""
    buf = _buf_pool.get()
    try:
        total = 0
        pos = offset
        remaining = count
        while remaining > 0:
            to_read = min(chunk_size, remaining)
            os.lseek(in_fd, pos, os.SEEK_SET)
            n = os.read(in_fd, buf, to_read)
            if n == 0:
                break
            written = 0
            while written < n:
                w = os.write(out_fd, buf[written:n])
                written += w
            total += n
            pos += n
            remaining -= n
        return total
    finally:
        _buf_pool.put(buf)


# ---------------------------------------------------------------------------
# Buffer pool — reuse memory buffers across requests
# ---------------------------------------------------------------------------

class _BufferPool:
    """Thread-safe pool of pre-allocated byte buffers.

    Reduces allocation pressure on hot paths by reusing buffers.
    Buffers larger than max_size are discarded (no unbounded growth).
    """

    def __init__(self, buf_size: int = 1024 * 1024, max_pool: int = 64):
        self._buf_size = buf_size
        self._max_pool = max_pool
        self._pool: list[memoryview] = []
        self._lock = threading.Lock()

    def get(self) -> memoryview:
        with self._lock:
            if self._pool:
                return self._pool.pop()
        return memoryview(bytearray(self._buf_size))

    def put(self, buf: memoryview) -> None:
        if buf.nbytes != self._buf_size:
            return  # Discard resized buffers
        with self._lock:
            if len(self._pool) < self._max_pool:
                self._pool.append(buf)


_buf_pool = _BufferPool()


# ---------------------------------------------------------------------------
# mmap read — zero-copy read for small files
# ---------------------------------------------------------------------------

def mmap_read(path: str, offset: int = 0, length: int = 0) -> bytes:
    """Read file contents using mmap. Best for small files (< 1MB).

    mmap avoids the read() syscall by mapping the file directly into
    virtual memory. The OS handles page faults transparently.

    Returns bytes object (copies out of mmap for safety).
    """
    fd = open_noatime(path)
    try:
        fstat = os.fstat(fd)
        file_size = fstat.st_size
        if file_size == 0:
            return b""

        if length == 0:
            length = file_size - offset

        # Limit mmap to reasonable size — don't mmap huge files
        if length > 4 * 1024 * 1024:
            # Too large for mmap, use regular read
            os.lseek(fd, offset, os.SEEK_SET)
            return os.read(fd, length)

        fadvise_random(fd)
        with mmap.mmap(fd, length, access=mmap.ACCESS_READ, offset=offset) as mm:
            return bytes(mm)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# High-level serve functions
# ---------------------------------------------------------------------------

def serve_file_range(
    path: str,
    start: int,
    end: int,
    out_fd: int,
    chunk_size: int = 1024 * 1024,
) -> int:
    """Serve a byte range from a file to an output fd using sendfile.

    Uses O_NOATIME + posix_fadvise(RANDOM) + sendfile for maximum efficiency.

    Returns bytes sent.
    """
    in_fd = open_noatime(path)
    try:
        fadvise_random(in_fd)
        length = end - start + 1
        return sendfile_chunk(out_fd, in_fd, start, length, chunk_size)
    finally:
        os.close(in_fd)


def serve_file_full(
    path: str,
    out_fd: int,
    chunk_size: int = 1024 * 1024,
) -> int:
    """Serve an entire file to an output fd using sendfile.

    Uses O_NOATIME + posix_fadvise(SEQUENTIAL) for streaming reads.

    Returns bytes sent.
    """
    in_fd = open_noatime(path)
    try:
        fadvise_sequential(in_fd)
        file_size = os.fstat(in_fd).st_size
        return sendfile_chunk(out_fd, in_fd, 0, file_size, chunk_size)
    finally:
        os.close(in_fd)


def read_file_optimized(path: str, max_size: int = 64 * 1024) -> bytes:
    """Read a file with optimal strategy based on size.

    - Small files (<= max_size): mmap (zero syscall)
    - Medium files: pread (no seek overhead)
    - Large files: should use streaming, not this function

    Returns bytes.
    """
    fd = open_noatime(path)
    try:
        file_size = os.fstat(fd).st_size
        if file_size == 0:
            return b""

        if file_size <= max_size:
            # Small file: mmap is fastest (no read syscall)
            fadvise_random(fd)
            with mmap.mmap(fd, file_size, access=mmap.ACCESS_READ) as mm:
                return bytes(mm)

        # Medium file: pread (avoids seek + read = 1 syscall instead of 2)
        fadvise_random(fd)
        buf = bytearray(file_size)
        os.preadv(fd, [buf], 0)
        return buf
    finally:
        os.close(fd)
