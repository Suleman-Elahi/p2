"""Volume-level ACL model and permission check function."""
from asgiref.sync import sync_to_async
from django.contrib.auth.models import Group, User
from django.db import models
from django.db.models import Q

from p2.core.models import Volume


class VolumeACL(models.Model):
    """Volume-level access control entry.

    Replaces django-guardian per-object permissions with a single indexed
    query per request. Supports both user-level and group-level entries.
    """

    volume = models.ForeignKey(Volume, on_delete=models.CASCADE, related_name='acls')
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    group = models.ForeignKey(Group, on_delete=models.CASCADE, null=True, blank=True)
    permissions = models.JSONField(default=list)  # ["read", "write", "delete", "list", "admin"]

    class Meta:
        unique_together = [('volume', 'user'), ('volume', 'group')]
        indexes = [
            models.Index(fields=['volume', 'user']),
            models.Index(fields=['volume', 'group']),
        ]

    def __str__(self):
        subject = self.user or self.group
        return f"VolumeACL({self.volume.name}, {subject}, {self.permissions})"


async def has_volume_permission(user, volume: Volume, permission: str) -> bool:
    """Check if a user has a given permission on a volume."""
    from p2.s3.cache import get_cached_acl, set_cached_acl
    import logging
    LOGGER = logging.getLogger(__name__)
    
    # Public volumes allow anonymous read AND list
    if volume.public_read and permission in ("read", "list"):
        return True
    
    # Check user attributes directly (avoid sync_to_async issues)
    try:
        user_id = user.id
        is_authenticated = hasattr(user, 'is_authenticated') and bool(user.is_authenticated)
        is_superuser = hasattr(user, 'is_superuser') and user.is_superuser
        username = getattr(user, 'username', 'unknown')
    except Exception as e:
        LOGGER.warning("has_volume_permission: error accessing user attributes: %s", e)
        return False
    
    if not is_authenticated:
        LOGGER.debug("has_volume_permission: user '%s' not authenticated", username)
        return False
    
    # Superusers have full access to all volumes
    if is_superuser:
        LOGGER.info("has_volume_permission: superuser '%s' granted '%s' on volume '%s'", username, permission, volume.name)
        return True
    
    # Use pk (uuid) for cache key since Volume uses UUID as primary key
    volume_pk = str(volume.pk)

    # Check cache
    cached = get_cached_acl(user_id, volume_pk, permission)
    if cached is not None:
        return cached
    
    # Cache miss - query database
    group_ids = [gid async for gid in Group.objects.filter(user=user).values_list('pk', flat=True).aiterator()]
    acls = VolumeACL.objects.filter(
        Q(user=user) | Q(group__in=group_ids),
        volume=volume,
    )
    allowed = False
    async for acl in acls:
        if permission in acl.permissions:
            allowed = True
            break
    
    set_cached_acl(user_id, volume_pk, permission, allowed)
    return allowed
