"""Helper to resolve the configured storage root."""
import os
import threading

from django.conf import settings

_KNOWN_DIRS: set[str] = set()
_DIR_LOCK = threading.Lock()


def storage_root() -> str:
    root = getattr(settings, 'STORAGE_ROOT', '/storage')
    return os.path.abspath(root)


def storage_path(*parts) -> str:
    """Join parts relative to the configured storage root."""
    return os.path.join(storage_root(), *parts)


def internal_to_fs(internal_path: str) -> str:
    """Convert an /internal-storage/... path to a real filesystem path."""
    return internal_path.replace('/internal-storage/', storage_root().rstrip('/') + '/')


def blob_shard_parts(blob_uuid: str) -> tuple[str, ...]:
    """Return shard directory parts for a blob UUID based on configured depth."""
    depth = int(getattr(settings, 'S3_BLOB_SHARD_DEPTH', 2))
    if depth <= 1:
        return (blob_uuid[0:2],)
    return (blob_uuid[0:2], blob_uuid[2:4])


def blob_dir(volume_uuid: str, blob_uuid: str) -> str:
    """Return filesystem directory path for blob payload placement."""
    return storage_path("volumes", volume_uuid, *blob_shard_parts(blob_uuid))


def blob_internal_path(volume_uuid: str, blob_uuid: str) -> str:
    """Return internal Nginx path for blob payload."""
    parts = "/".join(blob_shard_parts(blob_uuid))
    return f"/internal-storage/volumes/{volume_uuid}/{parts}/{blob_uuid}"


def blob_fs_path(volume_uuid: str, blob_uuid: str) -> str:
    """Return filesystem payload path for a blob UUID."""
    return os.path.join(blob_dir(volume_uuid, blob_uuid), blob_uuid)


def ensure_dir(path: str) -> None:
    """Create a directory once per process and cache successful creations."""
    if path in _KNOWN_DIRS:
        return
    with _DIR_LOCK:
        if path in _KNOWN_DIRS:
            return
        os.makedirs(path, exist_ok=True)
        _KNOWN_DIRS.add(path)
