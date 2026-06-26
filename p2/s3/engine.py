"""Process-level Engine registry using LMDB.

LMDB allows multiple discrete OS processes (e.g. Uvicorn workers) to securely memory
map the exact same database files simultaneously. It provides lock-free, zero-copy reads
across all concurrent workers while perfectly fulfilling the pure B+Tree key-value 
performance demands required for Petabyte-scale bucket sizes.
"""
import os
import threading
import lmdb
from django.conf import settings

_cache: dict = {}
_lock = threading.Lock()

class LMDbEngine:
    """Wrapper that acts as the S3 meta engine matching the old redb API natively."""
    def __init__(self, db_path: str):
        # Extract volume UUID hex from db_path
        try:
            self.volume_uuid_hex = os.path.basename(os.path.dirname(db_path))
        except Exception:
            self.volume_uuid_hex = None

        lmdb_sync = bool(getattr(settings, "S3_METADATA_LMDB_SYNC", True))
        lmdb_metasync = bool(getattr(settings, "S3_METADATA_LMDB_METASYNC", True))
        # map_size is the maximum virtual mapping size (1TB map limit here).
        # It allocates ZERO physical disk or RAM until values are actually inserted.
        self.env = lmdb.open(
            db_path, 
            max_dbs=1,
            map_size=256 * 1024 * 1024 * 1024,  # 256 GiB virtual map per Volume
            subdir=False,
            lock=True,
            max_readers=1024,   # Default 126 is too low for 8 workers × concurrent reqs
            readahead=False,    # Disable OS readahead — objects are random-access, not sequential
            meminit=False,      # Skip zero-filling new pages — saves CPU on writes
            sync=lmdb_sync,     # When false, commits avoid fsync on each write (higher throughput, lower durability)
            metasync=lmdb_metasync,
        )
        self.db = self.env.open_db(b"objects")

    def invalidate_cache(self, path: str) -> None:
        if not self.volume_uuid_hex:
            return
        try:
            from django.core.cache import cache
            parts = path.split('/')
            prefix = ""
            for part in parts[:-1]:
                prefix += part + "/"
                cache_key = f"folder_stats:{self.volume_uuid_hex}:{prefix}"
                cache.delete(cache_key)
        except Exception:
            pass

    def put(self, path: str, json_metadata: str) -> None:
        """Write key-value to LMDB."""
        with self.env.begin(write=True, db=self.db) as txn:
            txn.put(path.encode('utf-8'), json_metadata.encode('utf-8'))
        self.invalidate_cache(path)

    def put_raw(self, key: bytes, value: bytes) -> None:
        """Write raw bytes key-value to LMDB."""
        with self.env.begin(write=True, db=self.db) as txn:
            txn.put(key, value)

    def get(self, path: str) -> str | None:
        """Retrieve key-value from LMDB using lock-free read."""
        with self.env.begin(db=self.db) as txn:
            val = txn.get(path.encode('utf-8'))
            return val.decode('utf-8') if val else None

    def get_raw(self, key: bytes) -> bytes | None:
        """Retrieve raw bytes key-value from LMDB."""
        with self.env.begin(db=self.db) as txn:
            return txn.get(key)

    def delete(self, path: str) -> None:
        """Delete key from LMDB."""
        with self.env.begin(write=True, db=self.db) as txn:
            txn.delete(path.encode('utf-8'))
        self.invalidate_cache(path)

    def list(self, prefix: str, start_after: str | None = None, max_keys: int | None = 1000) -> list[tuple[str, str]]:
        """Scan keys matching `prefix` in LMDB B-Tree efficiently."""
        limit = max_keys if max_keys is not None else float('inf')
        results = []
        prefix_bytes = prefix.encode('utf-8')
        
        start_key = prefix
        check_start_after = False
        if start_after and start_after > start_key:
            start_key = start_after
            check_start_after = True
            
        start_key_bytes = start_key.encode('utf-8')
        if not start_key_bytes:
            start_key_bytes = b"\x01"

        with self.env.begin(db=self.db) as txn:
            cursor = txn.cursor()
            if cursor.set_range(start_key_bytes):
                for key, value in cursor:
                    if not key.startswith(prefix_bytes):
                        break
                    
                    if key.startswith(b'\x00'):
                        continue
                    
                    if check_start_after and key == start_after.encode('utf-8'):
                        continue # start_after is exclusive in S3
                        
                    results.append((key.decode('utf-8'), value.decode('utf-8')))
                    if len(results) >= limit:
                        break
        return results

    def list_dir(self, prefix: str, delimiter: str = "/", start_after: str | None = None, max_keys: int = 1000) -> 'tuple[list[tuple[str, str]], list[str]]':
        """List direct files and subdirectories under `prefix` using cursor jumping.
        
        Returns:
            (objects, common_prefixes)
            where objects is a list of (key, metadata_json) tuples
            and common_prefixes is a list of folder prefix strings.
        """
        prefix_bytes = prefix.encode('utf-8')
        start_key = prefix
        check_start_after = False
        if start_after and start_after > start_key:
            if start_after.endswith('/'):
                start_key = start_after[:-1] + chr(ord('/') + 1)
            else:
                start_key = start_after
                check_start_after = True
            
        start_key_bytes = start_key.encode('utf-8')
        if not start_key_bytes:
            start_key_bytes = b"\x01"
        
        objects = []
        common_prefixes = set()
        plen = len(prefix)
        
        with self.env.begin(db=self.db) as txn:
            cursor = txn.cursor()
            if not cursor.set_range(start_key_bytes):
                return [], []
            
            while True:
                key_bytes = cursor.key()
                if not key_bytes:
                    break
                if not key_bytes.startswith(prefix_bytes):
                    break
                
                if key_bytes.startswith(b'\x00'):
                    if not cursor.next():
                        break
                    continue
                
                key = key_bytes.decode('utf-8')
                if check_start_after and key == start_after:
                    if not cursor.next():
                        break
                    continue
                
                remainder = key[plen:]
                if delimiter and delimiter in remainder:
                    # Subdirectory found
                    dir_name = remainder[:remainder.index(delimiter) + 1]
                    sub_prefix = prefix + dir_name
                    common_prefixes.add(sub_prefix)
                    
                    # Jump past this subdirectory
                    jump_key = sub_prefix[:-1] + chr(ord(delimiter) + 1)
                    if not cursor.set_range(jump_key.encode('utf-8')):
                        break
                else:
                    # Direct object
                    value_bytes = cursor.value()
                    objects.append((key, value_bytes.decode('utf-8')))
                    if len(objects) + len(common_prefixes) >= max_keys:
                        break
                    if not cursor.next():
                        break
                        
        return objects, sorted(list(common_prefixes))

_storage_root_cache: str | None = None

def get_engine(volume) -> LMDbEngine:
    """Return the cached LMDbEngine for *volume*, creating it if needed.

    Thread-safe. Safe to call from sync Django views and from
    async views (via sync_to_async / asgiref thread pool).
    """
    global _storage_root_cache
    if _storage_root_cache is None:
        from django.conf import settings
        root = getattr(settings, 'STORAGE_ROOT', '/storage')
        # Resolve relative paths to absolute so all workers open the same file
        _storage_root_cache = os.path.abspath(root)

    db_path = os.path.join(_storage_root_cache, "volumes", volume.uuid.hex, "metadata.lmdb")

    # Fast path — no lock needed for cache hits
    engine = _cache.get(db_path)
    if engine is not None:
        return engine

    with _lock:
        # Double-checked locking
        engine = _cache.get(db_path)
        if engine is not None:
            return engine
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        _cache[db_path] = LMDbEngine(db_path)
        return _cache[db_path]


def close_engine(volume_uuid_hex: str) -> None:
    """Close and remove the cached LMDB engine for a volume.

    Call this when a volume is deleted to release the mmap, file descriptors,
    and reader table slots. Safe to call if the engine was never opened.
    """
    global _storage_root_cache
    if _storage_root_cache is None:
        return
    db_path = os.path.join(_storage_root_cache, "volumes", volume_uuid_hex, "metadata.lmdb")
    with _lock:
        engine = _cache.pop(db_path, None)
        if engine is not None:
            try:
                engine.env.close()
            except Exception:
                pass
