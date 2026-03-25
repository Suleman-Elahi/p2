"""p2 users view"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import \
    PermissionRequiredMixin as DjangoPermissionListMixin
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib.auth.models import User
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import reverse
from django.utils.translation import gettext as _
from django.views.generic import DeleteView, ListView, UpdateView

from p2.iam.forms import UserForm
from p2.lib.views import CreateAssignPermView


class UserListView(LoginRequiredMixin, ListView):
    """List all Users the user has access to"""

    model = User
    permission_required = 'auth.view_user'
    ordering = 'username'
    paginate_by = 10

    def get_queryset(self, *args, **kwargs):
        return super().get_queryset(*args, **kwargs)


class UserCreateView(SuccessMessageMixin, DjangoPermissionListMixin, CreateAssignPermView):
    """Create new User"""

    model = User
    form_class = UserForm
    permission_required = 'auth.add_user'
    template_name = 'generic/form.html'
    success_message = _('Successfully created User')
    permissions = [
        'auth.view_user',
        'auth.change_user',
        'auth.delete_user',
    ]

    def form_valid(self, form):
        response = super().form_valid(form)
        self.object.set_unusable_password()
        self.object.save()
        return response

    def get_success_url(self):
        return reverse('p2_ui:iam-users-list')


class UserUpdateView(SuccessMessageMixin, PermissionRequiredMixin, UpdateView):
    """Update existing User"""

    model = User
    form_class = UserForm
    permission_required = 'auth.change_user'
    template_name = 'generic/form.html'
    success_message = _('Successfully updated User')

    def get_success_url(self):
        return reverse('p2_ui:iam-users-list')


class UserDeleteView(SuccessMessageMixin, PermissionRequiredMixin, DeleteView):
    """Delete User"""

    model = User
    permission_required = 'auth.delete_user'
    template_name = 'generic/delete.html'
    success_message = _('Successfully deleted User')

    def get_success_url(self):
        return reverse('p2_ui:iam-users-list')

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, self.success_message % obj.__dict__)
        return super().delete(request, *args, **kwargs)
