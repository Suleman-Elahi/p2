"""p2 API models"""
import random
import string

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext as _


def get_access_key():
    """Generate random string to use as access key"""
    letters = string.ascii_uppercase + string.digits
    return ''.join(random.SystemRandom().choice(letters) for i in range(20))

def get_secret_key():
    """Generate random string to use as secret key.
    
    Uses alphanumeric + safe punctuation only to avoid copy/paste issues
    with shell-sensitive characters like quotes, backslashes, backticks, etc.
    """
    # Safe chars that won't cause issues in shells, HTML, or config files
    letters = string.ascii_lowercase + string.ascii_uppercase + string.digits + '+/='
    return ''.join(random.SystemRandom().choice(letters) for i in range(40))

def _get_fernet():
    """Return a Fernet instance using settings.FERNET_KEY."""
    key = getattr(settings, 'FERNET_KEY', None)
    if not key:
        import sys
        if 'pytest' in sys.modules or 'test' in sys.argv:
            key = b'E1Z1p9R_R1PXZsV6fM0P-7K99J1B_DkH7g8YfT-0m2U='
        else:
            raise ValueError("FERNET_KEY is not configured in Django settings.")
    return Fernet(key)

class APIKey(models.Model):
    """API Key"""

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.TextField()
    access_key = models.CharField(max_length=20, default=get_access_key, unique=True)
    secret_key_encrypted = models.CharField(max_length=512, default='')

    def save(self, *args, **kwargs):
        """Auto-encrypt a generated secret if none has been set yet."""
        if not self.secret_key_encrypted:
            self.set_secret_key(get_secret_key())
        super().save(*args, **kwargs)

    def set_secret_key(self, raw: str) -> None:
        """Encrypt and store the raw secret key using Fernet."""
        f = _get_fernet()
        self.secret_key_encrypted = f.encrypt(raw.encode()).decode()

    def decrypt_secret_key(self) -> str:
        """Decrypt and return the raw secret key (used for AWS v4 HMAC computation)."""
        f = _get_fernet()
        return f.decrypt(self.secret_key_encrypted.encode()).decode()

    def __str__(self):
        return "API Key %s for user %s" % (self.name, self.user.username)

    class Meta:
        verbose_name = _('API Key')
        verbose_name_plural = _('API Keys')
