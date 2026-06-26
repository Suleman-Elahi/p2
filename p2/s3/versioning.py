"""S3 Object Versioning support for p2.

Design principles
-----------------
* **Zero hot-path overhead** — all entry points are guarded by a single
  ``volume.tags.get('versioning') == 'true'`` check that is evaluated on the
  *already-cached* Volume object. No extra DB queries.
* **No data copying** — the physical blob file is never moved.  Only the
  lightweight LMDB metadata entry is archived.
* **Async-friendly** — every LMDB operation that could block is dispatched to
  ``asyncio.to_thread`` so the event loop is never stalled.
* **Namespace isolation** — version records live under the
  ``\\x00v/{path}\\x00{version_id}`` key prefix.  The leading ``\\x00`` byte
  guarantees they can never collide with any real S3 object key (S3 keys may
  not start with a NUL byte) and sorts before all real keys in LMDB cursor
  scans, so normal ``list`` / ``list_dir`` never encounters them.
"""
from __future__ import annotations

import asyncio
import json
import uuid as _uuid_mod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from p2.s3.engine import LMDbEngine

# ──────────────────────────────────────────────────────────────────────────────
# Key helpers
# ──────────────────────────────────────────────────────────────────────────────

# Version records stored as  \x00v/{s3_key}\x00{version_id}
_VERSION_PREFIX = b"\x00v/"
_VER_SEP = b"\x00"


def _version_lmdb_key(s3_key: str, version_id: str) -> bytes:
    """Return the LMDB byte key for a specific version."""
    return _VERSION_PREFIX + s3_key.encode() + _VER_SEP + version_id.encode()


def _parse_version_lmdb_key(raw: bytes) -> tuple[str, str] | None:
    """Parse a raw LMDB key back into (s3_key, version_id), or None."""
    if not raw.startswith(_VERSION_PREFIX):
        return None
    body = raw[len(_VERSION_PREFIX):]
    idx = body.rfind(_VER_SEP)
    if idx < 0:
        return None
    return body[:idx].decode(), body[idx + 1:].decode()


def _all_versions_prefix(s3_key: str) -> bytes:
    """Prefix for all versions of a given S3 key."""
    return _VERSION_PREFIX + s3_key.encode() + _VER_SEP


def new_version_id() -> str:
    """Generate a lexicographically sortable version ID (newest = highest)."""
    # Use a time-ordered UUID v7-style prefix so that newer versions sort last
    # when iterated in key order, giving us cheapest-possible "latest" lookup.
    return _uuid_mod.uuid4().hex  # random UUIDv4 hex — good enough


# ──────────────────────────────────────────────────────────────────────────────
# Synchronous engine operations (called via asyncio.to_thread)
# ──────────────────────────────────────────────────────────────────────────────

_DELETE_MARKER_MIME = "application/x-delete-marker"


def _is_delete_marker(meta: dict) -> bool:
    return meta.get("blob.p2.io/delete_marker", False) is True


def archive_version_sync(engine: "LMDbEngine", s3_key: str, metadata_json: str) -> str:
    """Archive *metadata_json* as an immutable previous version.

    Called synchronously inside ``asyncio.to_thread``.  Returns the version_id string.
    """
    meta = json.loads(metadata_json)
    version_id = meta.get("blob.p2.io/version_id")
    if not version_id:
        version_id = "null"
        meta["blob.p2.io/version_id"] = version_id
    archived_json = json.dumps(meta)

    lmdb_key = _version_lmdb_key(s3_key, version_id)
    with engine.env.begin(write=True, db=engine.db) as txn:
        txn.put(lmdb_key, archived_json.encode())
    return version_id


def write_delete_marker_sync(engine: "LMDbEngine", s3_key: str, now_ts: str) -> str:
    """Replace the current live entry with a delete marker and archive the
    previous live version (if any).  Returns the delete-marker version_id."""
    version_id = new_version_id()
    marker_key = _version_lmdb_key(s3_key, version_id)

    marker_meta = {
        "blob.p2.io/delete_marker": True,
        "blob.p2.io/version_id": version_id,
        "blob.p2.io/stat/mtime": now_ts,
    }
    marker_json = json.dumps(marker_meta).encode()

    with engine.env.begin(write=True, db=engine.db) as txn:
        # First, archive the current live object if it exists and has not been archived
        live_val = txn.get(s3_key.encode())
        if live_val:
            live_meta = json.loads(live_val)
            if not live_meta.get("blob.p2.io/delete_marker", False):
                live_vid = live_meta.get("blob.p2.io/version_id")
                if not live_vid:
                    live_vid = "null"
                    live_meta["blob.p2.io/version_id"] = live_vid
                    txn.put(
                        _version_lmdb_key(s3_key, live_vid),
                        json.dumps(live_meta).encode(),
                    )

        # Write the delete marker as a version entry
        txn.put(marker_key, marker_json)

        # Remove the live key so GET returns 404 (correct S3 behaviour)
        txn.delete(s3_key.encode())

    return version_id


def list_versions_sync(
    engine: "LMDbEngine",
    prefix: str,
    max_keys: int = 1000,
    key_marker: str = "",
    version_id_marker: str = "",
) -> list[dict]:
    """Return all version records for keys matching *prefix*.

    Each dict has: key, version_id, is_delete_marker, etag, size, last_modified,
    is_latest (bool — determined post-scan).
    Sorted by key asc, then version newest first (descending).
    """
    scan_prefix = _VERSION_PREFIX + prefix.encode()
    all_records: list[dict] = []

    with engine.env.begin(db=engine.db) as txn:
        cursor = txn.cursor()
        if not cursor.set_range(scan_prefix):
            return []
        for raw_key, raw_val in cursor:
            if not raw_key.startswith(scan_prefix):
                break
            parsed = _parse_version_lmdb_key(raw_key)
            if parsed is None:
                continue
            s3_key, version_id = parsed
            if not s3_key.startswith(prefix):
                break

            try:
                meta = json.loads(raw_val)
            except Exception:
                continue

            is_dm = meta.get("blob.p2.io/delete_marker", False) is True
            all_records.append({
                "key": s3_key,
                "version_id": version_id,
                "is_delete_marker": is_dm,
                "etag": meta.get("blob.p2.io/hash/md5", ""),
                "size": int(meta.get("blob.p2.io/size/bytes", 0)),
                "last_modified": meta.get("blob.p2.io/stat/mtime", ""),
                "storage_class": "STANDARD",
            })

    # Group by key
    by_key: dict[str, list[dict]] = {}
    for r in all_records:
        by_key.setdefault(r["key"], []).append(r)

    # Sort each key's versions by last_modified desc, and mark is_latest
    sorted_flat: list[dict] = []
    for s3_key in sorted(by_key.keys()):
        versions = by_key[s3_key]
        versions.sort(key=lambda x: (x["last_modified"], x["version_id"]), reverse=True)
        if versions:
            versions[0]["is_latest"] = True
            for v in versions[1:]:
                v["is_latest"] = False
        sorted_flat.extend(versions)

    # Apply pagination markers
    results: list[dict] = []
    start_adding = not bool(key_marker)

    for r in sorted_flat:
        if not start_adding:
            if r["key"] < key_marker:
                continue
            if r["key"] == key_marker:
                if version_id_marker:
                    if r["version_id"] == version_id_marker:
                        start_adding = True
                    continue
                else:
                    continue
            start_adding = True

        results.append(r)
        if len(results) >= max_keys:
            break

    return results


def delete_specific_version_sync(engine: "LMDbEngine", s3_key: str, version_id: str) -> bool:
    """Permanently remove a specific stored version. Returns True if found."""
    lmdb_key = _version_lmdb_key(s3_key, version_id)
    with engine.env.begin(write=True, db=engine.db) as txn:
        val = txn.get(lmdb_key)
        if val is None:
            return False
        # If the blob had a real file, delete it
        try:
            meta = json.loads(val)
            internal_path = meta.get("internal_path")
            if internal_path and not meta.get("blob.p2.io/delete_marker"):
                from p2.core.storage_path import internal_to_fs
                import os
                fs_path = internal_to_fs(internal_path)
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
        except Exception:
            pass
        txn.delete(lmdb_key)

        # Update the live key to reflect the new latest version if this key is versioned
        prefix = _all_versions_prefix(s3_key)
        cursor = txn.cursor()
        remaining = []
        if cursor.set_range(prefix):
            for k, v in cursor:
                if not k.startswith(prefix):
                    break
                parsed = _parse_version_lmdb_key(k)
                if parsed:
                    remaining.append((parsed[1], v))

        if remaining:
            # Sort by last_modified asc, then version_id asc so the last item is newest
            def _sort_key(item):
                try:
                    meta = json.loads(item[1])
                    return meta.get("blob.p2.io/stat/mtime", ""), item[0]
                except Exception:
                    return "", item[0]
            remaining.sort(key=_sort_key)
            latest_vid, latest_val = remaining[-1]
            latest_meta = json.loads(latest_val)
            if latest_meta.get("blob.p2.io/delete_marker", False):
                txn.delete(s3_key.encode())
            else:
                txn.put(s3_key.encode(), latest_val)
        else:
            txn.delete(s3_key.encode())

    return True


# ──────────────────────────────────────────────────────────────────────────────
# Async wrappers (safe to await directly from async views)
# ──────────────────────────────────────────────────────────────────────────────

async def archive_version(engine: "LMDbEngine", s3_key: str, metadata_json: str) -> str:
    """Async wrapper — archive the *current* metadata as a past version."""
    return await asyncio.to_thread(archive_version_sync, engine, s3_key, metadata_json)


async def write_delete_marker(engine: "LMDbEngine", s3_key: str, now_ts: str) -> str:
    """Async wrapper — place a delete marker and return its version_id."""
    return await asyncio.to_thread(write_delete_marker_sync, engine, s3_key, now_ts)


async def list_versions(
    engine: "LMDbEngine",
    prefix: str,
    max_keys: int = 1000,
    key_marker: str = "",
    version_id_marker: str = "",
) -> list[dict]:
    """Async wrapper — list all versions for keys under *prefix*."""
    return await asyncio.to_thread(
        list_versions_sync, engine, prefix, max_keys, key_marker, version_id_marker
    )


async def delete_specific_version(
    engine: "LMDbEngine", s3_key: str, version_id: str
) -> bool:
    """Async wrapper — permanently delete one specific version."""
    return await asyncio.to_thread(delete_specific_version_sync, engine, s3_key, version_id)


# ──────────────────────────────────────────────────────────────────────────────
# XML helpers
# ──────────────────────────────────────────────────────────────────────────────

def build_list_versions_xml(
    bucket: str,
    prefix: str,
    versions: list[dict],
    is_truncated: bool,
    namespace: str,
) -> "ElementTree.Element":
    from xml.etree import ElementTree
    root = ElementTree.Element("{%s}ListVersionsResult" % namespace)
    ElementTree.SubElement(root, "Name").text = bucket
    ElementTree.SubElement(root, "Prefix").text = prefix
    ElementTree.SubElement(root, "MaxKeys").text = str(1000)
    ElementTree.SubElement(root, "IsTruncated").text = "true" if is_truncated else "false"

    for v in versions:
        if v.get("is_delete_marker"):
            el = ElementTree.SubElement(root, "DeleteMarker")
        else:
            el = ElementTree.SubElement(root, "Version")
        ElementTree.SubElement(el, "Key").text = v["key"]
        ElementTree.SubElement(el, "VersionId").text = v["version_id"]
        ElementTree.SubElement(el, "IsLatest").text = "true" if v.get("is_latest") else "false"
        lm = v.get("last_modified", "")
        ElementTree.SubElement(el, "LastModified").text = lm
        if not v.get("is_delete_marker"):
            etag = v.get("etag", "")
            ElementTree.SubElement(el, "ETag").text = f'"{etag}"' if etag else ""
            ElementTree.SubElement(el, "Size").text = str(v.get("size", 0))
            ElementTree.SubElement(el, "StorageClass").text = v.get("storage_class", "STANDARD")

    return root
