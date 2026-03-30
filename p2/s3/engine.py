"""Process-level MetaEngine registry.

redb holds an exclusive file lock per database, so only one MetaEngine
instance per path can exist in a process. This module provides a single
shared registry used by both the S3 views and the upload viewset.
"""
import os
import threading

from p2.s3 import p2_s3_meta

_cache: dict = {}
_lock = threading.Lock()


def get_engine(volume) -> p2_s3_meta.MetaEngine:
    """Return the cached MetaEngine for *volume*, creating it if needed.

    Thread-safe. Safe to call from sync Django views and from
    async views (via sync_to_async / asgiref thread pool).
    """
    dir_path = f"/storage/volumes/{volume.uuid.hex}"
    os.makedirs(dir_path, exist_ok=True)
    db_path = f"{dir_path}/metadata.redb"

    with _lock:
        if db_path not in _cache:
            _cache[db_path] = p2_s3_meta.MetaEngine(db_path)
        return _cache[db_path]
