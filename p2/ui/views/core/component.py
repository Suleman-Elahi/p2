"""Component Views"""
from django.contrib import messages
from django.contrib.auth.mixins import \
    PermissionRequiredMixin as DjangoPermissionRequiredMixin
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.http import Http404
from django.shortcuts import reverse
from django.utils.translation import gettext as _
from django.views.generic import DeleteView, UpdateView

from p2.core.models import Component
from p2.lib.reflection import path_to_class
from p2.lib.reflection.manager import ControllerManager
from p2.lib.views import CreateAssignPermView

COMPONENT_MANAGER = ControllerManager('component.controllers')


class ComponentCreateView(SuccessMessageMixin, DjangoPermissionRequiredMixin, CreateAssignPermView):
    """Create new component"""

    model = Component
    permission_required = 'p2_core.add_component'
    template_name = 'generic/form.html'
    success_message = _('Successfully created Component')
    permissions = [
        'p2_core.view_component',
        'p2_core.change_component',
        'p2_core.delete_component',
    ]

    volume = None
    controller_path = None

    def get_form_class(self):
        # We need to lookup the volume so we know which volume to assign the component to
        from p2.core.models import Volume
        self.volume = Volume.objects.filter(pk=self.kwargs.get('pk')).first()
        controller_path = self.request.GET.get('controller')
        if controller_path in COMPONENT_MANAGER:
            controller = path_to_class(controller_path)
            self.controller_path = controller_path
            return path_to_class(controller(Component(volume=self.volume)).form_class)
        raise Http404

    def form_valid(self, form):
        form.instance.volume = self.volume
        form.instance.controller_path = self.controller_path
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('p2_ui:core-volume-detail', kwargs={'pk': self.object.volume.pk})


class ComponentUpdateView(SuccessMessageMixin, PermissionRequiredMixin, UpdateView):
    """Update existing component"""

    model = Component
    permission_required = 'p2_core.change_component'
    template_name = 'generic/form.html'
    success_message = _('Successfully updated Component')

    def get_form_class(self):
        return path_to_class(self.object.controller.form_class)

    def get_form(self, form_class=None):
        """Return an instance of the form to be used in this view."""
        form = super().get_form(form_class=form_class)
        form.load(self.object)
        return form

    def get_success_url(self):
        return reverse('p2_ui:core-volume-detail', kwargs={'pk': self.object.volume.pk})


class ComponentDeleteView(SuccessMessageMixin, PermissionRequiredMixin, DeleteView):
    """Delete component"""

    model = Component
    permission_required = 'p2_core.delete_component'
    template_name = 'generic/delete.html'
    success_message = _('Successfully deleted Component')

    def get_success_url(self):
        return reverse('p2_ui:core-volume-detail', kwargs={'pk': self.object.volume.pk})

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, self.success_message % obj.__dict__)
        return super().delete(request, *args, **kwargs)
