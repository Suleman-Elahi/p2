"""p2 lib models"""
from uuid import uuid4

from django.db import models


class UUIDModel(models.Model):
    """Generic Model with a UUID as Primary key"""

    uuid = models.UUIDField(default=uuid4, primary_key=True)

    class Meta:

        abstract = True

class TagModel(models.Model):
    """Model which can be tagged and have pre-defined tag keys"""

    tags = models.JSONField(default=dict, blank=True)

    PREDEFINED_TAGS = {}
    REQUIRED_KEYS = []

    def get_predefined_tags(self):
        """Get list of pre-defined keys, which should be set as default"""
        return self.PREDEFINED_TAGS

    def get_required_keys(self):
        """Get list of required keys. Will be checked in form"""
        return self.REQUIRED_KEYS

    class Meta:

        abstract = True
