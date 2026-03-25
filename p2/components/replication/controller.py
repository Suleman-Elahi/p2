"""p2 replication controller"""
import copy
from shutil import copyfileobj
from time import time

import logging

from p2.components.replication.constants import (TAG_BLOB_SOURCE_UUID,
                                                 TAG_REPLICATION_TARGET)
from p2.core.components.base import ComponentController
from p2.core.models import Blob, Volume

LOGGER = logging.getLogger(__name__)

# pylint: disable=too-few-public-methods
class ReplicationController(ComponentController):
    """Replicate Blobs 1:1 between volumes"""

    template_name = 'components/replication/card.html'
    form_class = 'p2.components.replication.forms.ReplicationForm'

    @property
    def target_volume(self):
        """Get Target volume"""
        return Volume.objects.get(pk=self.instance.tags.get(TAG_REPLICATION_TARGET))

    def _get_target_blob(self, source_blob):
        target_volume = self.target_volume
        # Check if there's a blob that's our source UUID as attribute
        possible_targets = Blob.objects.filter(**{
            'volume': target_volume,
            'attributes__%s' % TAG_BLOB_SOURCE_UUID: source_blob.uuid.hex})
        if possible_targets.exists():
            target_blob = possible_targets.first()
            LOGGER.debug("Found existing target blob: %s", target_blob.uuid.hex)
        else:
            LOGGER.debug("Creating new replicated blob, source=%s", source_blob.uuid.hex)
            target_blob = Blob.objects.create(
                path=source_blob.path,
                volume=target_volume,
                attributes={
                    TAG_BLOB_SOURCE_UUID: source_blob.uuid.hex
                })
        return target_blob

    def full_replication(self, source_volume):
        """Full replication after component has been configured"""
        start_time = time()
        for blob in Blob.objects.filter(volume=source_volume):
            self.metadata_update(blob)
            target_blob = self.payload_update(blob)
            target_blob.save()
        end_time = time()
        space = self.target_volume.space_used_bytes
        duration = (end_time - start_time) + 1 # +1 to make sure we don't divide by 0
        rate = space / duration
        LOGGER.debug("Initial full replication finished, %r bytes per second", rate)

    def metadata_update(self, blob):
        """Replicate metadata save"""
        LOGGER.debug('Replicating::UpdateMetadata blob=%s', blob)
        target_blob = self._get_target_blob(blob)
        target_blob.path = blob.path
        for attr in ['path', 'prefix', 'attributes', 'tags']:
            # Copy all metadata to new blob
            setattr(target_blob, attr, copy.deepcopy(getattr(blob, attr)))
        # Make sure we don't erase the source uuid
        target_blob.attributes[TAG_BLOB_SOURCE_UUID] = blob.uuid.hex
        return target_blob

    def payload_update(self, blob):
        """Replicate payload update"""
        LOGGER.debug('Replicating::UpdatePayload blob=%s', blob)
        target_blob = self._get_target_blob(blob)
        copyfileobj(blob, target_blob)
        return target_blob

    def delete(self, blob):
        """Delete remote blob"""
        LOGGER.debug('Replicating::Delete blob=%s', blob)
        target_blob = self._get_target_blob(blob)
        target_blob.delete()
