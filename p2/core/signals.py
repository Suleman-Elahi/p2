"""p2 signals"""
from django.core.signals import Signal
from django.db.models import F
from django.db.models.functions import Greatest
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from p2.core import constants
from p2.core.models import Blob, Volume

import logging
LOGGER = logging.getLogger(__name__)

# BLOB_PRE_SAVE remains synchronous — quota check must block writes before commit.
BLOB_PRE_SAVE = Signal()
BLOB_ACCESS = Signal()


@receiver(pre_save, sender=Blob)
def blob_pre_save(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Trigger BLOB_PRE_SAVE (synchronous quota check)."""
    BLOB_PRE_SAVE.send(sender=sender, blob=instance)


@receiver(pre_delete, sender=Blob)
def blob_pre_delete(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Tell storage to delete blob and decrement space_used_bytes."""
    instance.volume.storage.controller.delete(instance)
    size = int(instance.attributes.get(constants.ATTR_BLOB_SIZE_BYTES, 0))
    if size > 0:
        Volume.objects.filter(pk=instance.volume_id).update(
            space_used_bytes=Greatest(F('space_used_bytes') - size, 0)
        )
