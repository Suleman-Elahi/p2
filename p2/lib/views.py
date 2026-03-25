"""p2 helper views"""

from django.views.generic import CreateView


class CreateAssignPermView(CreateView):
    """Create view — permissions are now managed via VolumeACL, not per-object guardian perms."""

    permissions = []

    def form_valid(self, form):
        return super().form_valid(form)
