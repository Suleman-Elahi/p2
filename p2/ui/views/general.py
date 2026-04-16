"""p2 UI Index view"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

from p2.core.models import Volume
from p2.ui.stats import get_volume_stats


class IndexView(LoginRequiredMixin, ListView):
    """Show overview of volumes"""

    model = Volume
    permission_required = 'p2_core.view_volume'
    template_name = 'general/index.html'
    ordering = 'name'
    paginate_by = 9

    def get_queryset(self, *args, **kwrags):
        return super().get_queryset(*args, **kwrags).select_related('storage')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        total_objects = 0

        for volume in data['object_list']:
            stats = get_volume_stats(volume)
            volume.object_count = stats['object_count']
            volume.space_used_bytes = stats['total_bytes']
            total_objects += stats['object_count']

        data['count'] = total_objects
        return data

class SearchView(LoginRequiredMixin, ListView):
    """Search Blobs by their key - currently disabled"""

    model = Volume
    ordering = 'name'
    template_name = 'search/results.html'

    def get_queryset(self):
        return Volume.objects.none()
