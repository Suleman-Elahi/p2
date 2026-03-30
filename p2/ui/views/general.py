"""p2 UI Index view"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.views.generic import ListView

from p2.core.models import Volume
from p2.ui.constants import CACHE_KEY_BLOB_COUNT


class IndexView(LoginRequiredMixin, ListView):
    """Show overview of volumes"""

    model = Volume
    permission_required = 'p2_core.view_volume'
    template_name = 'general/index.html'
    ordering = 'name'
    paginate_by = 9

    def get_queryset(self, *args, **kwrags):
        return super().get_queryset(*args, **kwrags).select_related('storage')

    def get_blob_count(self):
        return 0

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        data['count'] = self.get_blob_count()
        return data

class SearchView(LoginRequiredMixin, ListView):
    """Search Blobs by their key - currently disabled"""

    model = Volume
    ordering = 'name'
    template_name = 'search/results.html'

    def get_queryset(self):
        return Volume.objects.none()
