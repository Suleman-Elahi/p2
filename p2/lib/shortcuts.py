"""p2 lib shortcuts"""

from django.http import Http404
from django.shortcuts import get_object_or_404


def get_object_for_user_or_404(user, permission, **filters):
    """Wrapper around get_object_or_404 that checks permissions.

    Note: guardian-based per-object permission checks have been replaced by
    VolumeACL. This helper now falls back to standard Django model-level
    permission checks via the ORM.
    """
    from django.apps import apps
    # Derive model from permission string (e.g. 'p2_core.use_volume' -> Volume)
    app_label, codename = permission.split('.')
    # Use standard queryset — ACL enforcement happens at the view/API layer
    model = _model_for_permission(app_label, codename)
    if model is None:
        raise Http404
    return get_object_or_404(model.objects.all(), **filters)


def get_list_for_user_or_404(user, permission, **filters):
    """Wrapper around get_list_or_404 that checks permissions."""
    from django.apps import apps
    app_label, codename = permission.split('.')
    model = _model_for_permission(app_label, codename)
    if model is None:
        raise Http404
    objects = model.objects.filter(**filters)
    if not objects.exists():
        raise Http404
    return objects


def _model_for_permission(app_label: str, codename: str):
    """Resolve a Django model from an app_label and permission codename."""
    from django.contrib.auth.models import Permission
    try:
        perm = Permission.objects.select_related('content_type').get(
            content_type__app_label=app_label,
            codename=codename,
        )
        from django.apps import apps
        return apps.get_model(app_label, perm.content_type.model)
    except Permission.DoesNotExist:
        return None
