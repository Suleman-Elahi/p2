"""p2 log models"""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext as _

from p2.lib.models import TagModel, UUIDModel
from p2.lib.reflection import path_to_class
from p2.lib.reflection.manager import ControllerManager

LOG_CONTROLLER_MANAGER = ControllerManager('log.controllers', lazy=True)

class LogAdaptor(TagModel, UUIDModel):
    """Base Log Adaptor, doesn't write to anything"""

    name = models.TextField()
    controller_path = models.TextField(choices=LOG_CONTROLLER_MANAGER.as_choices())

    _controller_instance = None

    @property
    def controller(self):
        """Get instantiated controller class"""
        if not self._controller_instance:
            controller_class = path_to_class(self.controller_path)
            self._controller_instance = controller_class(self)
        return self._controller_instance

    def __str__(self):
        return "Log Adaptor %s" % self.name

    class Meta:

        verbose_name = _('Log Adaptor')
        verbose_name_plural = _('Log Adaptors')

class Record(UUIDModel):
    """Log record from DatabaseLogAdaptor"""

    adaptor = models.ForeignKey(LogAdaptor, on_delete=models.CASCADE)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)
    request_uid = models.UUIDField(blank=True, null=True)
    body = models.JSONField(default=dict, encoder=DjangoJSONEncoder)

    def __str__(self):
        body_string = ' '.join(["%s=%s" % kv for kv in self.body.items()])
        return "[%s] [%s] %s" % (self.start_time, self.request_uid.hex, body_string)

    class Meta:

        verbose_name = _('Log Record')
        verbose_name_plural = _('Log Records')
