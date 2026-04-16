"""Helpers for persisted per-volume object and byte counters."""
import json

from django.db.models import F, Value
from django.db.models.functions import Greatest

from p2.core.constants import ATTR_BLOB_IS_FOLDER, ATTR_BLOB_SIZE_BYTES
from p2.core.models import Volume
from p2.s3.engine import get_engine

STATS_INITIALIZED_TAG = "p2.ui.stats_initialized"


async def adjust_volume_stats(volume, object_delta=0, bytes_delta=0):
    """Atomically adjust persisted counters for a volume."""
    await Volume.objects.filter(pk=volume.pk).aupdate(
        object_count=Greatest(Value(0), F("object_count") + Value(object_delta)),
        space_used_bytes=Greatest(Value(0), F("space_used_bytes") + Value(bytes_delta)),
    )

    if not volume.tags.get(STATS_INITIALIZED_TAG):
        volume.tags[STATS_INITIALIZED_TAG] = True
        await volume.asave(update_fields=["tags"])


def adjust_volume_stats_sync(volume, object_delta=0, bytes_delta=0):
    """Sync wrapper for request paths that are still synchronous."""
    Volume.objects.filter(pk=volume.pk).update(
        object_count=Greatest(Value(0), F("object_count") + Value(object_delta)),
        space_used_bytes=Greatest(Value(0), F("space_used_bytes") + Value(bytes_delta)),
    )

    if not volume.tags.get(STATS_INITIALIZED_TAG):
        volume.tags[STATS_INITIALIZED_TAG] = True
        volume.save(update_fields=["tags"])


def scan_volume_stats(volume):
    """Scan LMDB metadata once to derive counters for a volume."""
    engine = get_engine(volume)
    object_count = 0
    total_bytes = 0

    for _, metadata_json in engine.list('', None, None):
        try:
            attributes = json.loads(metadata_json)
        except (TypeError, ValueError):
            continue

        if attributes.get(ATTR_BLOB_IS_FOLDER, False):
            continue

        object_count += 1
        total_bytes += int(attributes.get(ATTR_BLOB_SIZE_BYTES, 0) or 0)

    return object_count, total_bytes


def recalculate_volume_stats(volume):
    """Recompute and persist counters for a volume."""
    object_count, total_bytes = scan_volume_stats(volume)
    volume.object_count = object_count
    volume.space_used_bytes = total_bytes
    volume.tags[STATS_INITIALIZED_TAG] = True
    volume.save(update_fields=["object_count", "space_used_bytes", "tags"])
    return object_count, total_bytes
