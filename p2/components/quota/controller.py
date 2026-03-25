"""p2 quota controller"""
import logging

from p2.components.quota.constants import (ACTION_BLOCK, ACTION_EMAIL,
                                           ACTION_NOTHING, TAG_QUOTA_ACTION,
                                           TAG_QUOTA_THRESHOLD)
from p2.components.quota.exceptions import QuotaExceededException
from p2.core.components.base import ComponentController
from p2.core.constants import ATTR_BLOB_SIZE_BYTES

LOGGER = logging.getLogger(__name__)

# pylint: disable=too-few-public-methods
class QuotaController(ComponentController):
    """Quota controller"""

    template_name = 'components/quota/card.html'
    form_class = 'p2.components.quota.forms.QuotaForm'

    def before_save(self, blob):
        """Check if new blob would be over threshold"""
        new_blob_size = int(blob.attributes[ATTR_BLOB_SIZE_BYTES])
        if (self.volume.space_used_bytes + new_blob_size) > self.threshold:
            # We'd be over, so execute our action and raise an Exception to prevent saving.
            self.do_action(blob)

    def do_action(self, blob):
        """Execute action"""
        LOGGER.info("Blob %r is pushing us over the threshold.", blob)
        action = self.instance.tags.get(TAG_QUOTA_ACTION, ACTION_NOTHING)
        LOGGER.info("Action: %s", action)
        if action == ACTION_NOTHING:
            return
        if action == ACTION_BLOCK:
            raise QuotaExceededException
        if action == ACTION_EMAIL:
            # TODO: Send Email
            pass

    @property
    def threshold(self):
        """Get threshold"""
        return int(self.instance.tags.get(TAG_QUOTA_THRESHOLD, 0))

    @property
    def quota_percentage(self):
        """Check if volume is close to any quota"""
        threshold = int(self.instance.tags.get(TAG_QUOTA_THRESHOLD, 0))
        if threshold == 0:
            return 0
        return self.volume.space_used_bytes / (threshold / 100)
