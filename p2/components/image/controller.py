"""p2 import controller"""
from PIL import ExifTags, Image
import logging

from p2.components.image.constants import TAG_IMAGE_EXIF_TAGS
from p2.core.components.base import ComponentController

LOGGER = logging.getLogger(__name__)

# pylint: disable=too-few-public-methods
class ImageController(ComponentController):
    """add image attributes from exif data"""

    template_name = 'components/image/card.html'
    form_class = 'p2.components.image.forms.ImageForm'

    def handle(self, blob):
        """extract EXIF data from image and save as attributes"""
        try:
            img = Image.open(blob)
            LOGGER.debug("Removing stale EXIF keys")
            # Remove all keys starting with EXIF: to prevent stale keys
            for key in list(blob.attributes.keys()):
                if key.startswith('blob.p2.io/exif/'):
                    del blob.attributes[key]
            # Read out EXIF attributes, lookup numerical EXIF name and save as key blob.p2.io/exif/x
            # pylint: disable=protected-access
            if hasattr(img, '_getexif'):
                LOGGER.debug("Adding exif from file")
                # pylint: disable=protected-access
                exif_data = img._getexif()
                if exif_data:
                    new_attributes = self.get_attributes(exif_data)
                    blob.attributes.update(new_attributes)
                    LOGGER.debug("Updated EXIF data")
            blob.save()
        except IOError as exc:
            LOGGER.debug(exc)
            LOGGER.debug("Blob is not an image, skipping EXIF.", blob=blob)

    def get_attributes(self, raw_exif):
        """Convert raw exif data into usable tags"""
        allowed_tags = self.instance.tags.get(TAG_IMAGE_EXIF_TAGS, [])
        attributes = {}
        for key, value in raw_exif.items():
            if key not in ExifTags.TAGS:
                continue
            if not isinstance(value, str):
                continue
            tag_name = ExifTags.TAGS[key]
            if tag_name not in allowed_tags:
                continue
            attributes['blob.p2.io/exif/%s' % ExifTags.TAGS[key]] = value
        return attributes
