"""Data migration: backfill space_used_bytes for all volumes from aggregate query.

Validates: Requirements 5.7, 6.1
"""
import logging

from django.db import migrations
from django.db.models import Sum

LOGGER = logging.getLogger(__name__)

ATTR_BLOB_SIZE_BYTES = 'blob.p2.io/size/bytes'


def backfill_space_used(apps, schema_editor):
    """Set space_used_bytes on each volume by summing blob size attributes."""
    Volume = apps.get_model('p2_core', 'Volume')
    Blob = apps.get_model('p2_core', 'Blob')

    for volume in Volume.objects.all():
        total = 0
        for blob in Blob.objects.filter(volume=volume):
            size_str = blob.attributes.get(ATTR_BLOB_SIZE_BYTES, '0')
            try:
                total += int(size_str)
            except (ValueError, TypeError):
                pass
        Volume.objects.filter(pk=volume.pk).update(space_used_bytes=total)
        LOGGER.info("Backfilled space_used_bytes=%d for volume %s", total, volume.name)


def reverse_backfill_space_used(apps, schema_editor):
    """Reset space_used_bytes to 0 (reversible no-op)."""
    Volume = apps.get_model('p2_core', 'Volume')
    Volume.objects.all().update(space_used_bytes=0)


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0027_volumeacl'),
    ]

    operations = [
        migrations.RunPython(
            backfill_space_used,
            reverse_code=reverse_backfill_space_used,
        ),
    ]
