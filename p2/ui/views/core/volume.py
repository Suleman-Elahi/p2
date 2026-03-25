"""Volume Views"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import \
    PermissionRequiredMixin as DjangoPermissionRequiredMixin
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import reverse
from django.utils.translation import gettext as _
from django.views.generic import DeleteView, DetailView, ListView, UpdateView

from p2.core.forms import VolumeForm
from p2.core.models import Component, Volume
from p2.lib.reflection import class_to_path
from p2.lib.reflection.manager import ControllerManager
from p2.lib.views import CreateAssignPermView

COMPONENT_MANAGER = ControllerManager('component.controllers')


class VolumeListView(LoginRequiredMixin, ListView):
    """List all volumes a user can use"""

    model = Volume
    permission_required = 'p2_core.view_volume'
    ordering = 'name'
    paginate_by = 10


class VolumeDetailView(PermissionRequiredMixin, DetailView):
    """Show volume overview and all components activated/available"""

    model = Volume
    permission_required = 'p2_core.view_volume'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        components = []
        existing_components = Component.objects.filter(volume=self.object)
        for controller in COMPONENT_MANAGER.list():
            controller_path = class_to_path(controller)
            # Check if component for this volume is configure
            existing_component = existing_components.filter(
                controller_path=controller_path,
                volume=self.object)
            if existing_component.exists():
                components.append(existing_component.first())
            else:
                # Create an in-memory Object with the controller_path assigned
                _component = Component()
                _component.controller_path = controller_path
                _component.volume = self.object
                # Set an extra attribute so the template can reflect it
                _component.configured = False
                _component.enabled = False
                components.append(_component)

        context['components'] = components
        return context


class VolumeCreateView(SuccessMessageMixin, DjangoPermissionRequiredMixin, CreateAssignPermView):
    """Create new volume"""

    model = Volume
    form_class = VolumeForm
    permission_required = 'p2_core.add_volume'
    template_name = 'generic/form.html'
    success_message = _('Successfully created Volume')
    permissions = [
        'p2_core.view_volume',
        'p2_core.use_volume',
        'p2_core.change_volume',
        'p2_core.delete_volume',
    ]

    def get_success_url(self):
        return reverse('p2_ui:core-volume-list')

class VolumeUpdateView(SuccessMessageMixin, PermissionRequiredMixin, UpdateView):
    """Update existing volume"""

    model = Volume
    form_class = VolumeForm
    permission_required = 'p2_core.change_volume'
    template_name = 'generic/form.html'
    success_message = _('Successfully updated Volume')

    def get_success_url(self):
        return reverse('p2_ui:core-volume-list')

class VolumeDeleteView(SuccessMessageMixin, PermissionRequiredMixin, DeleteView):
    """Delete volume"""

    model = Volume
    permission_required = 'p2_core.delete_volume'
    template_name = 'generic/delete.html'
    success_message = _('Successfully deleted Volume')

    def get_success_url(self):
        return reverse('p2_ui:core-volume-list')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, self.success_message % obj.__dict__)
        return super().delete(request, *args, **kwargs)
