"""Data migration: convert django-guardian UserObjectPermission entries to VolumeACL.

Skipped gracefully if guardian is not installed.

Validates: Requirements 5.7
"""
import logging

from django.db import migrations

LOGGER = logging.getLogger(__name__)

# Map guardian codenames to VolumeACL permission strings
GUARDIAN_PERM_MAP = {
    'view_volume': 'read',
    'change_volume': 'write',
    'delete_volume': 'delete',
    'list_volume_contents': 'list',
    'use_volume': 'admin',
}


def guardian_to_volumeacl(apps, schema_editor):
    """Migrate guardian UserObjectPermission rows to VolumeACL entries."""
    try:
        from guardian.models import UserObjectPermission  # noqa: PLC0415
    except ImportError:
        LOGGER.warning(
            "django-guardian is not installed; skipping guardian → VolumeACL migration."
        )
        return

    Volume = apps.get_model('p2_core', 'Volume')
    VolumeACL = apps.get_model('p2_core', 'VolumeACL')

    volume_ct_id = None
    try:
        from django.contrib.contenttypes.models import ContentType  # noqa: PLC0415
        volume_ct = ContentType.objects.get_for_model(Volume)
        volume_ct_id = volume_ct.pk
    except Exception:  # pylint: disable=broad-except
        LOGGER.warning("Could not resolve ContentType for Volume; skipping migration.")
        return

    uop_qs = UserObjectPermission.objects.filter(
        content_type_id=volume_ct_id,
    ).select_related('permission', 'user')

    migrated = 0
    for uop in uop_qs:
        codename = uop.permission.codename
        p2_perm = GUARDIAN_PERM_MAP.get(codename)
        if not p2_perm:
            LOGGER.debug("Skipping unknown guardian permission codename: %s", codename)
            continue

        try:
            volume = Volume.objects.get(pk=uop.object_pk)
        except Volume.DoesNotExist:
            LOGGER.debug("Volume pk=%s not found; skipping.", uop.object_pk)
            continue

        acl, created = VolumeACL.objects.get_or_create(
            volume=volume,
            user=uop.user,
            group=None,
            defaults={'permissions': [p2_perm]},
        )
        if not created and p2_perm not in acl.permissions:
            acl.permissions = acl.permissions + [p2_perm]
            acl.save(update_fields=['permissions'])

        migrated += 1

    LOGGER.info("Migrated %d guardian UserObjectPermission(s) to VolumeACL.", migrated)


def reverse_guardian_to_volumeacl(apps, schema_editor):
    """No-op reverse: VolumeACL entries created here are left in place."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0029_data_public_read'),
    ]

    operations = [
        migrations.RunPython(
            guardian_to_volumeacl,
            reverse_code=reverse_guardian_to_volumeacl,
        ),
    ]
