"""p2 expiry controller"""
from time import time

import logging

from p2.components.expire.constants import TAG_EXPIRE_DATE
from p2.core.components.base import ComponentController
from p2.core.models import Blob

LOGGER = logging.getLogger(__name__)

# pylint: disable=too-few-public-methods
class ExpiryController(ComponentController):
    """Add permissions to blob to be publicly accessible"""

    template_name = 'components/expiry/card.html'
    # No custom form needed
    form_class = 'p2.core.components.forms.ComponentForm'

    def expire_volume(self, volume):
        """Delete blobs from Volume which are expired"""
        for blob in Blob.objects.filter(**{
                'volume': volume,
                'tags__%s__isnull' % TAG_EXPIRE_DATE: False
            }):
            if int(time()) >= blob.tags[TAG_EXPIRE_DATE]:
                LOGGER.debug("%r needs to be expired!", blob)
                blob.delete()
