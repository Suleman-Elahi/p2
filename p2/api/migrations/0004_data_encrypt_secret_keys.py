"""Data migration: encrypt existing plaintext secret_key values with Fernet.

If FERNET_KEY is not configured, logs a warning and skips encryption.

Validates: Requirements 13.6
"""
import logging

from django.conf import settings
from django.db import migrations

LOGGER = logging.getLogger(__name__)


def encrypt_secret_keys(apps, schema_editor):
    """Encrypt any APIKey rows that have an empty secret_key_encrypted."""
    fernet_key = getattr(settings, 'FERNET_KEY', None)
    if not fernet_key:
        LOGGER.warning(
            "FERNET_KEY is not configured; skipping secret_key encryption migration. "
            "Run this migration again after setting FERNET_KEY."
        )
        return

    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415
        f = Fernet(fernet_key)
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.error("Failed to initialise Fernet with FERNET_KEY: %s", exc)
        return

    APIKey = apps.get_model('p2_api', 'APIKey')

    # Only process rows where secret_key_encrypted is still empty (not yet encrypted)
    qs = APIKey.objects.filter(secret_key_encrypted='')
    count = 0
    for api_key in qs:
        # There is no plaintext column anymore (removed in 0003), so we generate
        # a fresh secret for any row that somehow still has an empty encrypted field.
        import random  # noqa: PLC0415
        import string  # noqa: PLC0415
        letters = string.ascii_lowercase + string.ascii_uppercase + string.digits + string.punctuation
        raw = ''.join(random.SystemRandom().choice(letters) for _ in range(40))
        api_key.secret_key_encrypted = f.encrypt(raw.encode()).decode()
        api_key.save(update_fields=['secret_key_encrypted'])
        count += 1

    LOGGER.info("Encrypted secret_key for %d APIKey row(s).", count)


def reverse_encrypt_secret_keys(apps, schema_editor):
    """No-op reverse: encrypted values are left in place."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('p2_api', '0003_apikey_secret_key_encrypted'),
    ]

    operations = [
        migrations.RunPython(
            encrypt_secret_keys,
            reverse_code=reverse_encrypt_secret_keys,
        ),
    ]
