"""Data migration: set public_read=True for volumes with PublicAccessController enabled.

Validates: Requirements 5.7
"""
import logging

from django.db import migrations

LOGGER = logging.getLogger(__name__)

PUBLIC_ACCESS_CONTROLLER_PATH = 'p2.components.public_access.controller.PublicAccessController'


def set_public_read(apps, schema_editor):
    """Enable public_read on volumes that have a PublicAccessController component."""
    Volume = apps.get_model('p2_core', 'Volume')
    Component = apps.get_model('p2_core', 'Component')

    volume_pks = Component.objects.filter(
        controller_path=PUBLIC_ACCESS_CONTROLLER_PATH,
        enabled=True,
    ).values_list('volume_id', flat=True)

    updated = Volume.objects.filter(pk__in=volume_pks).update(public_read=True)
    LOGGER.info("Set public_read=True on %d volume(s)", updated)


def reverse_set_public_read(apps, schema_editor):
    """Reset public_read to False (reversible no-op)."""
    Volume = apps.get_model('p2_core', 'Volume')
    Volume.objects.all().update(public_read=False)


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0028_data_backfill_space_used'),
    ]

    operations = [
        migrations.RunPython(
            set_public_read,
            reverse_code=reverse_set_public_read,
        ),
    ]
