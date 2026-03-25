"""APIKey views"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import \
    PermissionRequiredMixin as DjangoPermissionListMixin
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import reverse
from django.utils.translation import gettext as _
from django.views.generic import DeleteView, ListView, UpdateView

from p2.api.forms import APIKeyForm
from p2.api.models import APIKey
from p2.lib.views import CreateAssignPermView


class APIKeyListView(LoginRequiredMixin, ListView):
    """List all access keys the user has access to"""

    model = APIKey
    permission_required = 'p2_api.view_apikey'
    ordering = 'name'
    paginate_by = 10


class APIKeyCreateView(SuccessMessageMixin, DjangoPermissionListMixin, CreateAssignPermView):
    """Create new access key"""

    model = APIKey
    form_class = APIKeyForm
    permission_required = 'p2_api.add_apikey'
    template_name = 'generic/form.html'
    success_message = _('Successfully created API Key')
    permissions = [
        'p2_api.view_apikey',
        'p2_api.change_apikey',
        'p2_api.delete_apikey',
    ]

    def form_valid(self, form):
        response = super().form_valid(form)
        secret = self.object.decrypt_secret_key()
        messages.warning(
            self.request,
            f'Secret Key (shown once): {secret}'
        )
        return response

    def get_success_url(self):
        return reverse('p2_ui:api-key-list')

class APIKeyUpdateView(SuccessMessageMixin, PermissionRequiredMixin, UpdateView):
    """Update existing access key"""

    model = APIKey
    form_class = APIKeyForm
    permission_required = 'p2_api.change_apikey'
    template_name = 'generic/form.html'
    success_message = _('Successfully updated API Key')

    def get_success_url(self):
        return reverse('p2_ui:api-key-list')

class APIKeyDeleteView(SuccessMessageMixin, PermissionRequiredMixin, DeleteView):
    """Delete access key"""

    model = APIKey
    permission_required = 'p2_api.delete_apikey'
    template_name = 'generic/delete.html'
    success_message = _('Successfully deleted API Key')

    def get_success_url(self):
        return reverse('p2_ui:api-key-list')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, self.success_message % obj.__dict__)
        return super().delete(request, *args, **kwargs)
