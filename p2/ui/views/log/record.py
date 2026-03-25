"""log record views"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

from p2.lib.shortcuts import (get_list_for_user_or_404,
                              get_object_for_user_or_404)
from p2.log.models import Record


class RecordListView(LoginRequiredMixin, ListView):
    """List all log records the user has access to"""

    model = Record
    permission_required = 'p2_log.view_record'
    ordering = '-start_time'
    paginate_by = 10

    def get_queryset(self):
        adaptor = get_object_for_user_or_404(
            self.request.user, 'p2_log.view_logadaptor', pk=self.kwargs.get('pk'))
        self.extra_context = {
            'adaptor': adaptor
        }
        return get_list_for_user_or_404(
            self.request.user, 'p2_log.view_record', adaptor=adaptor).order_by(self.ordering)
