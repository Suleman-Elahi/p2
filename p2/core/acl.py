"""Volume-level ACL model and permission check function."""
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
    if volume.public_read and permission == "read":
        return True
    # AnonymousUser has no groups and can never have ACL entries
    if not user or not user.is_authenticated:
        return False
    group_ids = [gid async for gid in Group.objects.filter(user=user).values_list('pk', flat=True).aiterator()]
    return await VolumeACL.objects.filter(
        Q(user=user) | Q(group__in=group_ids),
        volume=volume,
        permissions__contains=[permission],
    ).aexists()
