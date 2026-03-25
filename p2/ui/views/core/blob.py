"""Blob Views"""
import posixpath

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import reverse
from django.utils.translation import gettext as _
from django.views.generic import DeleteView, DetailView, ListView, UpdateView

from p2.core.forms import BlobForm
from p2.core.http import BlobResponse
from p2.core.models import Blob
from p2.core.prefix_helper import PrefixHelper, make_absolute_prefix
from p2.lib.shortcuts import get_object_for_user_or_404


class FileBrowserView(LoginRequiredMixin, ListView):
    """List all blobs a user has access to"""

    template_name = 'p2_core/blob_list.html'
    model = Blob
    permission_required = 'p2_core.view_blob'
    ordering = 'path'
    paginate_by = 20

    prefix = ''

    def get_queryset(self):
        self.prefix = make_absolute_prefix(self.request.GET.get('prefix', '/'))
        volume = get_object_for_user_or_404(
            self.request.user, 'p2_core.use_volume', pk=self.kwargs.get('pk'))
        return super().get_queryset().filter(
            prefix=self.prefix,
            volume=volume)

    def get_context_data(self, **kwargs):
        kwargs['volume'] = get_object_for_user_or_404(
            self.request.user, 'p2_core.use_volume', pk=self.kwargs.get('pk'))
        kwargs['breadcrumbs'] = []
        current_total = []
        # Remove first slash so we don't get an empty leading breadcrumb
        for path_part in self.prefix[1:].split(posixpath.sep):
            current_total.append(path_part)
            kwargs['breadcrumbs'].append({
                'part': path_part,
                'full': '/'.join(current_total)
            })
        return super().get_context_data(**kwargs)


    #     helper = PrefixHelper(self.request.user, context['volume'], prefix)
    #     if prefix != '/':
    #         helper.add_up_prefix()
    #     helper.collect(max_levels=1)
    #     context['prefixes'] = helper.prefixes
    #     context['breadcrumbs'] = helper.get_breadcrumbs()

    #     page = self.request.GET.get('page', 1)
    #     objects_per_page = 20

    #     paginator = Paginator(blobs, objects_per_page)
    #     context['objects'] = paginator.get_page(page)

        # return context

class BlobDetailView(PermissionRequiredMixin, DetailView):
    """View Blob Details"""

    model = Blob
    permission_required = 'p2_core.view_blob'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        helper = PrefixHelper(self.request.user, self.object.volume, self.object.prefix)
        helper.collect(max_levels=1)
        context['breadcrumbs'] = helper.get_breadcrumbs()
        return context

class BlobUpdateView(SuccessMessageMixin, PermissionRequiredMixin, UpdateView):
    """Update blob"""

    model = Blob
    form_class = BlobForm
    permission_required = 'p2_core.change_blob'
    template_name = 'generic/form.html'
    success_message = _('Successfully updated Blob')

    def get_success_url(self):
        return reverse('p2_ui:core-blob-list', kwargs={'pk': self.object.volume.pk})


class BlobDeleteView(SuccessMessageMixin, PermissionRequiredMixin, DeleteView):
    """Delete blob"""

    model = Blob
    permission_required = 'p2_core.delete_blob'
    template_name = 'generic/delete.html'
    success_message = _('Successfully deleted Blob')

    def get_success_url(self):
        return reverse('p2_ui:core-blob-list', kwargs={'pk': self.object.volume.pk})

    def delete(self, request, *args, **kwargs):
        obj = self.get_object()
        messages.success(self.request, self.success_message % obj.__dict__)
        return super().delete(request, *args, **kwargs)


class BlobDownloadView(PermissionRequiredMixin, DetailView):
    """Download blob's payload"""

    model = Blob
    permission_required = 'p2_core.view_blob'

    def get(self, *args, **kwargs):
        super().get(*args, **kwargs)
        return BlobResponse(self.object)
