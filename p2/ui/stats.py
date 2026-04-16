"""Helpers for deriving UI stats with one-time backfill for legacy volumes."""
from p2.core.volume_stats import STATS_INITIALIZED_TAG, recalculate_volume_stats


def get_volume_stats(volume):
    """Return object count and bytes for a volume.

    Fast path: persisted counters on the Volume row.
    Legacy path: one-time LMDB scan, then persist the result.
    """
    if volume.tags.get(STATS_INITIALIZED_TAG):
        return {
            "object_count": volume.object_count,
            "total_bytes": volume.space_used_bytes,
        }

    object_count, total_bytes = recalculate_volume_stats(volume)

    return {
        "object_count": object_count,
        "total_bytes": total_bytes,
    }
