"""p2 expiry controller

NOTE: The Blob ORM model has been removed. expire_volume() has been stubbed
out — expiry will be reimplemented using the p2_s3_meta LSM engine by scanning
the redb metadata store for entries whose TAG_EXPIRE_DATE has passed.
"""
import logging

from p2.core.components.base import ComponentController

LOGGER = logging.getLogger(__name__)


# pylint: disable=too-few-public-methods
class ExpiryController(ComponentController):
    """Add permissions to blob to be publicly accessible"""

    template_name = 'components/expiry/card.html'
    form_class = 'p2.core.components.forms.ComponentForm'

    def expire_volume(self, volume):
        """Delete expired objects from Volume — stubbed pending LSM reimplementation."""
        LOGGER.warning(
            "ExpiryController.expire_volume: stubbed (Blob model removed). "
            "Expiry for volume %s skipped.", volume.pk
        )
