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
