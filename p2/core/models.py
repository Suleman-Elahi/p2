"""p2 Core models"""
import posixpath

from asgiref.sync import async_to_sync
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils.timezone import now
from django.utils.translation import gettext as _
import logging

from p2.core.constants import (ATTR_BLOB_IS_FOLDER, ATTR_BLOB_SIZE_BYTES,
                               ATTR_BLOB_STAT_CTIME, ATTR_BLOB_STAT_MTIME)
from p2.core.events import (STREAM_BLOB_PAYLOAD_UPDATED, STREAM_BLOB_POST_SAVE,
                             make_event, publish_event)
from p2.core.prefix_helper import make_absolute_path, make_absolute_prefix
from p2.core.validators import validate_blob_path
from p2.lib.models import TagModel, UUIDModel
from p2.lib.reflection import class_to_path, path_to_class
from p2.lib.reflection.manager import ControllerManager

LOGGER = logging.getLogger(__name__)
STORAGE_MANAGER = ControllerManager('storage.controllers', lazy=True)
COMPONENT_MANAGER = ControllerManager('component.controllers', lazy=True)


class Volume(UUIDModel, TagModel):
    """Folder-like object, holding a collection of blobs"""

    name = models.SlugField(unique=True, max_length=63)
    storage = models.ForeignKey('Storage', on_delete=models.CASCADE)
    space_used_bytes = models.BigIntegerField(default=0)
    public_read = models.BooleanField(default=False)

    def component(self, class_or_path):
        """Get component instance for class or class path.
        Return None if component not confugued."""
        if not isinstance(class_or_path, str):
            class_or_path = class_to_path(class_or_path)
        component = self.component_set.filter(
            controller_path=class_or_path,
            enabled=True)
        if component.exists():
            return component.first()
        return None

    def __str__(self):
        return f"Volume {self.name} on {self.storage.name}"

    class Meta:

        verbose_name = _('Volume')
        verbose_name_plural = _('Volumes')
        permissions = (
            ('list_volume_contents', 'Can List contents'),
            ('use_volume', 'Can Use Volume')
        )


# pylint: disable=too-many-instance-attributes
class Blob(UUIDModel, TagModel):
    """Binary-large object, member of a Volume and store in the volume's storage"""

    path = models.TextField(validators=[validate_blob_path])
    prefix = models.TextField()

    volume = models.ForeignKey('Volume', on_delete=models.CASCADE)
    attributes = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    _writing_handle = None
    _reading_handle = None

    class Meta:

        verbose_name = _('Blob')
        verbose_name_plural = _('Blobs')
        unique_together = (('path', 'volume',),)

    @staticmethod
    def from_uploaded_file(file, volume, prefix=posixpath.sep):
        """Create Blob instance from Django's UploadedFile"""
        blob = Blob(
            path=posixpath.join(prefix, file.name),
            volume=volume)
        for chunk in file.chunks():
            blob.write(chunk)
        blob.attributes[ATTR_BLOB_SIZE_BYTES] = str(file.size)
        blob.save()
        return blob

    @property
    def filename(self):
        """Return only the filename part of self.path"""
        if ATTR_BLOB_IS_FOLDER not in self.attributes:
            return posixpath.basename(self.path)
        return posixpath.basename(posixpath.normpath(self.path))

    def _open_read_handle(self):
        if not self._reading_handle:
            self._reading_handle = self.volume.storage.controller.get_read_handle(self)

    ### File-like methods

    def read(self, *args, **kwargs):
        """Retrieve file from storage controller and call read method. Accepts same
        arguments as file's read."""
        self._open_read_handle()
        return self._reading_handle.read(*args, **kwargs)

    def readline(self, *args, **kwargs):
        """Retrieve file from storage controller and call readline method. Accepts same
        arguments as file's readline."""
        self._open_read_handle()
        return self._reading_handle.readline(*args, **kwargs)

    def write(self, *args, **kwargs):
        """Open TemporaryFile for writing, method arguments are the same as file's write.
        File is not committed until Blob.save() is called."""
        if not self._writing_handle:
            self._writing_handle = self.volume.storage.controller.get_write_handle(self)
        return self._writing_handle.write(*args, **kwargs)

    def seek(self, pos):
        """Seek open handles to position"""
        self._open_read_handle()
        self._reading_handle.seek(pos)
        if self._writing_handle:
            self._writing_handle.seek(pos)

    def tell(self):
        """Return current position of write handle if open, otherwise read handle"""
        self._open_read_handle()
        if self._writing_handle:
            return self._writing_handle.tell()
        return self._reading_handle.tell()

    def __update_prefix(self):
        self.prefix = make_absolute_prefix(posixpath.dirname(self.path))
        if ATTR_BLOB_IS_FOLDER in self.attributes:
            self.path = make_absolute_prefix(self.path)

    def __failsafe_path(self):
        """Make sure no path collisions can happen"""
        same_path = Blob.objects.filter(path=self.path, volume=self.volume)
        if same_path.exists() and same_path.first() != self:
            self.path = self.path + '.1'
            self.__failsafe_path()
        self.path = make_absolute_path(self.path)

    def save(self, *args, **kwargs):
        """Name file on storage after generated UUID and populate initial attributes"""
        # Save current time as `created` attribute. This can be changed by users,
        # but p2.log creates a log entry for new Blob being created
        if ATTR_BLOB_STAT_CTIME not in self.attributes:
            self.attributes[ATTR_BLOB_STAT_CTIME] = now()
        # Create/update `date_updated` attribute
        self.attributes[ATTR_BLOB_STAT_MTIME] = now()
        # Only save payload if it changed
        if self._writing_handle:
            self._writing_handle.seek(0)
            self.volume.storage.controller.commit(self, self._writing_handle)
        # Check if path exists already
        self.__failsafe_path()
        # Update prefix
        self.__update_prefix()
        self.volume.storage.controller.collect_attributes(self)
        super().save(*args, **kwargs)
        if self._writing_handle:
            # Atomically increment space_used_bytes after payload is committed and attributes collected
            size = int(self.attributes.get(ATTR_BLOB_SIZE_BYTES, 0))
            if size > 0:
                Volume.objects.filter(pk=self.volume_id).update(
                    space_used_bytes=F('space_used_bytes') + size
                )
        # Publish post-save event asynchronously via Redis Streams
        _publish = async_to_sync(publish_event)
        _publish(STREAM_BLOB_POST_SAVE, make_event(
            blob_uuid=self.uuid.hex,
            volume_uuid=self.volume.uuid.hex,
            event_type='blob_post_save',
        ))
        if self._writing_handle:
            # Publish payload-updated event only when payload changed
            _publish(STREAM_BLOB_PAYLOAD_UPDATED, make_event(
                blob_uuid=self.uuid.hex,
                volume_uuid=self.volume.uuid.hex,
                event_type='blob_payload_updated',
            ))

    def __str__(self):
        return f"{self.volume.name}:/{self.path}"


class Storage(UUIDModel, TagModel):
    """Storage instance which stores blob instances."""

    name = models.TextField()
    controller_path = models.TextField(choices=STORAGE_MANAGER.as_choices())

    _controller_instance = None

    @property
    def controller(self):
        """Get instantiated controller class"""
        if not self._controller_instance:
            controller_class = path_to_class(self.controller_path)
            self._controller_instance = controller_class(self)
        return self._controller_instance

    def get_required_keys(self):
        return self.controller.get_required_tags()

    def __str__(self):
        return f"Storage {self.name}"

    class Meta:

        verbose_name = _('Storage')
        verbose_name_plural = _('Storages')
        permissions = (
            ('use_storage', 'Can use storage'),
        )


class Component(UUIDModel, TagModel):
    """Pluggable component instance connection volume to ComponentController"""

    enabled = models.BooleanField(default=True)
    configured = True
    volume = models.ForeignKey('Volume', on_delete=models.CASCADE)
    controller_path = models.TextField(choices=COMPONENT_MANAGER.as_choices())

    _controller_instance = None

    @property
    def controller(self):
        """Get instantiated controller class"""
        if not self._controller_instance:
            controller_class = path_to_class(self.controller_path)
            try:
                self._controller_instance = controller_class(self)
            except (TypeError, ImportError) as exc:
                LOGGER.warning(exc)
        return self._controller_instance

    def __str__(self):
        return f"{self.controller.__class__.__name__} for {self.volume.name}"

    class Meta:

        verbose_name = _('Component')
        verbose_name_plural = _('Components')
        unique_together = (('volume', 'controller_path',),)
