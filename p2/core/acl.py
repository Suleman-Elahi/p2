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


async def has_volume_permission(user: User, volume: Volume, permission: str) -> bool:
    """Check if a user has a given permission on a volume.

    Returns True if:
    - The volume has public_read=True and permission is "read", OR
    - There is a VolumeACL entry granting the permission to the user directly
      or via one of the user's groups.

    Returns False (default-deny) if no matching ACL entry exists.

    Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6
    """
    if volume.public_read and permission == "read":
        return True
    return await VolumeACL.objects.filter(
        Q(user=user) | Q(group__in=await user.agroups.all()),
        volume=volume,
        permissions__contains=[permission],
    ).aexists()
