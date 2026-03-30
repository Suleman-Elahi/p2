"""p2 replication controller

NOTE: Full replication logic depended on the Blob ORM model which has now been
removed. Methods that operated on individual blobs (metadata_update, payload_update,
_get_target_blob, delete) are stubbed out with a NotImplementedError so the class
still satisfies the ComponentController interface.  full_replication() is also
stubbed since it iterated over Blob.objects.

These will be reimplemented against the p2_s3_meta LSM engine in a future pass.
"""
import logging

from p2.components.replication.constants import TAG_REPLICATION_TARGET
from p2.core.components.base import ComponentController
from p2.core.models import Volume

LOGGER = logging.getLogger(__name__)


# pylint: disable=too-few-public-methods
class ReplicationController(ComponentController):
    """Replicate objects between volumes (partially stubbed for LSM migration)."""

    template_name = 'components/replication/card.html'
    form_class = 'p2.components.replication.forms.ReplicationForm'

    @property
    def target_volume(self):
        """Get Target volume."""
        return Volume.objects.get(pk=self.instance.tags.get(TAG_REPLICATION_TARGET))

    def _get_target_blob(self, source_blob):
        raise NotImplementedError("ReplicationController: Blob model removed — not yet reimplemented")

    def full_replication(self, source_volume):
        """Full replication — stubbed pending LSM reimplementation."""
        LOGGER.warning("ReplicationController.full_replication: stubbed (Blob model removed)")

    def metadata_update(self, blob):
        """Replicate metadata — stubbed pending LSM reimplementation."""
        raise NotImplementedError("ReplicationController: Blob model removed — not yet reimplemented")

    def payload_update(self, blob):
        """Replicate payload — stubbed pending LSM reimplementation."""
        raise NotImplementedError("ReplicationController: Blob model removed — not yet reimplemented")

    def delete(self, blob):
        """Delete remote blob — stubbed pending LSM reimplementation."""
        LOGGER.warning("ReplicationController.delete: stubbed (Blob model removed)")
