"""Management command to recalculate space_used_bytes for all volumes

NOTE: This command is currently a no-op stub. The Blob model has been replaced
by the p2_s3_meta LSM engine. Volume space tracking now happens inline in the
PutObject view (objects.py). A future version of this command will scan the
redb metadata store to recompute totals.
"""

from django.core.management.base import BaseCommand

from p2.core.models import Volume


class Command(BaseCommand):
    """Recalculate space_used_bytes for all volumes (stubbed)"""

    help = "Recalculate space_used_bytes for all volumes (currently a no-op — Blob model removed)"

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.WARNING(
                "recalculate_space_used is currently a no-op. "
                "The Blob model has been replaced by the p2_s3_meta LSM engine. "
                "Space tracking happens inline during PutObject."
            )
        )
        count = Volume.objects.count()
        self.stdout.write(f"Found {count} volume(s) — no recalculation performed.")
        self.stdout.write(self.style.SUCCESS("Done."))
