"""p2 public_access controller — DEPRECATED.

Public access is now controlled via Volume.public_read (a BooleanField).
This controller is retained as a no-op stub so existing Component rows
in the database do not cause import errors. New volumes should use
Volume.public_read=True instead of creating a PublicAccessController component.
"""
import logging

from p2.core.components.base import ComponentController

LOGGER = logging.getLogger(__name__)


# pylint: disable=too-few-public-methods
class PublicAccessController(ComponentController):
    """Stub: public access is now controlled via Volume.public_read."""

    template_name = 'components/public_access/card.html'
    form_class = 'p2.core.components.forms.ComponentForm'

    def add_permissions(self, blob):
        """No-op: set Volume.public_read=True instead."""
        LOGGER.warning(
            "PublicAccessController.add_permissions called on blob %s — "
            "use Volume.public_read=True instead.",
            blob.pk,
        )
