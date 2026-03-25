"""Data migration: remove SHA1, SHA384, SHA512 hash attributes from all blobs.

Only MD5 and SHA256 are retained going forward.

Validates: Requirements 7.3
"""
import logging

from django.db import migrations

LOGGER = logging.getLogger(__name__)

OLD_HASH_KEYS = [
    'blob.p2.io/hash/sha1',
    'blob.p2.io/hash/sha384',
    'blob.p2.io/hash/sha512',
]


def remove_old_hashes(apps, schema_editor):
    """Strip legacy hash attributes from blob JSON."""
    Blob = apps.get_model('p2_core', 'Blob')

    # Process blobs that have at least one of the old hash keys
    qs = Blob.objects.filter(attributes__has_key='blob.p2.io/hash/sha1')
    # Also catch blobs that only have sha384/sha512 but not sha1
    qs384 = Blob.objects.filter(attributes__has_key='blob.p2.io/hash/sha384')
    qs512 = Blob.objects.filter(attributes__has_key='blob.p2.io/hash/sha512')

    affected_pks = set(
        list(qs.values_list('pk', flat=True)) +
        list(qs384.values_list('pk', flat=True)) +
        list(qs512.values_list('pk', flat=True))
    )

    updated = 0
    for blob in Blob.objects.filter(pk__in=affected_pks):
        changed = False
        for key in OLD_HASH_KEYS:
            if key in blob.attributes:
                del blob.attributes[key]
                changed = True
        if changed:
            blob.save(update_fields=['attributes'])
            updated += 1

    LOGGER.info("Removed old hash attributes from %d blob(s).", updated)


def reverse_remove_old_hashes(apps, schema_editor):
    """No-op reverse: removed hash values cannot be recovered."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('p2_core', '0030_data_guardian_to_volumeacl'),
    ]

    operations = [
        migrations.RunPython(
            remove_old_hashes,
            reverse_code=reverse_remove_old_hashes,
        ),
    ]
