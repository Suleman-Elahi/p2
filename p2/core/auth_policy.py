"""Auth-policy model and resolution logic.

A super-admin can set an org-wide default login requirement (or per-Group
overrides).  ``resolve_policy(user)`` returns the strictest applicable
requirement — callers in the login flow use it to gate access.

Part of p2.core (no new INSTALLED_APPS entry).
"""
import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import models

LOGGER = logging.getLogger(__name__)
User = get_user_model()


class AuthPolicy(models.Model):
    """Org-wide default (group=NULL) or per-Group login-method override."""

    class Requirement(models.TextChoices):
        NONE = 'none', 'No requirement (password alone is allowed)'
        MFA_REQUIRED = 'mfa_required', 'Password + TOTP 2FA required'
        SSO_REQUIRED = 'sso_required', 'SSO only (password login disabled)'
        SSO_OR_MFA = 'sso_or_mfa', 'SSO or password+2FA (bare password forbidden)'

    group = models.OneToOneField(
        Group,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='auth_policy',
        help_text='NULL = org-wide default. Exactly one row may have group=NULL.',
    )
    requirement = models.CharField(
        max_length=20,
        choices=Requirement.choices,
        default=Requirement.NONE,
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['group'],
                condition=models.Q(group__isnull=True),
                name='authpolicy_one_global_default',
            ),
        ]

    def __str__(self):
        return f"AuthPolicy({self.group or 'GLOBAL'} -> {self.requirement})"


# Strictness ordering — strictest wins when a user is in multiple groups.
_STRICTNESS = {
    AuthPolicy.Requirement.NONE: 0,
    AuthPolicy.Requirement.MFA_REQUIRED: 1,
    AuthPolicy.Requirement.SSO_OR_MFA: 1,
    AuthPolicy.Requirement.SSO_REQUIRED: 2,
}


def resolve_policy(user) -> str:
    """Return the effective requirement for *user*.

    Resolution order:
      1. Collect requirements from every Group the user belongs to.
      2. Fall back to the global default (group IS NULL), or NONE.
      3. Pick the strictest.

    Superuser exemption is handled by the *caller*, not here — keeping
    this function purely "what would apply" makes it testable.
    """
    group_reqs = list(
        AuthPolicy.objects.filter(group__in=user.groups.all())
        .values_list('requirement', flat=True)
    )
    global_req = (
        AuthPolicy.objects.filter(group__isnull=True)
        .values_list('requirement', flat=True)
        .first()
    ) or AuthPolicy.Requirement.NONE

    candidates = group_reqs + [global_req]
    return max(candidates, key=lambda r: _STRICTNESS.get(r, 0))
