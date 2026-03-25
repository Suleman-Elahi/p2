"""log log-adaptor views"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import \
    PermissionRequiredMixin as DjangoPermissionListMixin
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import reverse
from django.utils.translation import gettext as _
from django.views.generic import DeleteView, ListView, UpdateView

from p2.lib.reflection import path_to_class
from p2.lib.reflection.manager import ControllerManager
from p2.lib.views import CreateAssignPermView
from p2.log.forms import LogAdaptorForm
from p2.log.models import LogAdaptor

LOG_CONTROLLER_MANAGER = ControllerManager('log.controllers', lazy=True)


class LogAdaptorListView(LoginRequiredMixin, ListView):
    """List all access keys the user has access to"""

    model = LogAdaptor
    permission_required = 'p2_log.view_logadaptor'
    ordering = 'name'
    paginate_by = 10


class LogAdaptorCreateView(SuccessMessageMixin, DjangoPermissionListMixin, CreateAssignPermView):
    """Create new access key"""

    model = LogAdaptor
    form_class = LogAdaptorForm
    permission_required = 'p2_log.add_logadaptor'
    template_name = 'generic/form.html'
    success_message = _('Successfully created LogAdaptor')
    permissions = [
        'p2_log.view_logadaptor',
        'p2_log.change_logadaptor',
        'p2_log.delete_logadaptor',
    ]

    def get_success_url(self):
        return reverse('p2_ui:log-adaptor-list')


class LogAdaptorUpdateView(SuccessMessageMixin, PermissionRequiredMixin, UpdateView):
    """Update existing access key"""

    model = LogAdaptor
    permission_required = 'p2_log.change_logadaptor'
    template_name = 'generic/form.html'
    success_message = _('Successfully updated LogAdaptor')

    controller_path = None

    def get_form_class(self):
        return path_to_class(self.object.controller.form_class)

    def get_success_url(self):
        return reverse('p2_ui:log-adaptor-list')


class LogAdaptorDeleteView(SuccessMessageMixin, PermissionRequiredMixin, DeleteView):
    """Delete access key"""

    model = LogAdaptor
    permission_required = 'p2_log.delete_logadaptor'
    template_name = 'generic/delete.html'
    success_message = _('Successfully deleted LogAdaptor')

    def get_success_url(self):
        return reverse('p2_ui:log-adaptor-list')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, self.success_message % obj.__dict__)
        return super().delete(request, *args, **kwargs)
