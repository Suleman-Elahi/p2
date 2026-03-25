"""Management command to recalculate space_used_bytes for all volumes"""

from django.core.management.base import BaseCommand

from p2.core.constants import ATTR_BLOB_SIZE_BYTES
from p2.core.models import Blob, Volume


class Command(BaseCommand):
    """Recalculate space_used_bytes for all volumes from actual blob sizes"""

    help = "Recalculate space_used_bytes for all volumes from actual blob sizes"

    def handle(self, *args, **options):
        volumes = Volume.objects.all()
        total = volumes.count()
        self.stdout.write(f"Recalculating space_used_bytes for {total} volume(s)...")

        for i, volume in enumerate(volumes, start=1):
            blobs = Blob.objects.filter(volume=volume)
            total_bytes = 0
            for blob in blobs:
                size = blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
                try:
                    total_bytes += int(size)
                except (ValueError, TypeError):
                    pass

            Volume.objects.filter(pk=volume.pk).update(space_used_bytes=total_bytes)
            self.stdout.write(
                f"  [{i}/{total}] Volume '{volume.name}': "
                f"set space_used_bytes = {total_bytes}"
            )

        self.stdout.write(self.style.SUCCESS("Done."))
