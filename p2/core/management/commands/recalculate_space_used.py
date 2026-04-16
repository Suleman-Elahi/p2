"""Recalculate persisted per-volume object and byte counters from LMDB metadata."""

from django.core.management.base import BaseCommand

from p2.core.models import Volume
from p2.core.volume_stats import recalculate_volume_stats


class Command(BaseCommand):
    """Recalculate object_count and space_used_bytes for all volumes."""

    help = "Recalculate object_count and space_used_bytes for all volumes from LMDB metadata"

    def handle(self, *args, **options):
        count = Volume.objects.count()
        self.stdout.write(f"Found {count} volume(s) — recalculating counters...")
        for volume in Volume.objects.order_by("name"):
            object_count, total_bytes = recalculate_volume_stats(volume)
            self.stdout.write(
                f"{volume.name}: {object_count} object(s), {total_bytes} bytes"
            )
        self.stdout.write(self.style.SUCCESS("Done."))
