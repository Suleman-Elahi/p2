"""Auth-policy CRUD API — superuser-only management of login requirements."""
import logging
from typing import List, Optional

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError

from p2.core.auth_policy import AuthPolicy, resolve_policy

LOGGER = logging.getLogger(__name__)
User = get_user_model()

router_auth_policy = Router(tags=['system-auth-policy'])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AuthPolicySchema(Schema):
    id: int
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    requirement: str
    updated_at: str


class AuthPolicyCreateSchema(Schema):
    group_id: Optional[int] = None
    requirement: str


class AuthPolicyUpdateSchema(Schema):
    requirement: str
    confirm_password: Optional[str] = None


class AffectedCountSchema(Schema):
    count: int


class UserPolicySchema(Schema):
    user_id: int
    username: str
    effective_policy: str


class GroupListSchema(Schema):
    id: int
    name: str
    user_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_superuser(request):
    if not getattr(request.user, 'is_superuser', False):
        raise PermissionDenied('Superuser privileges required.')


def _policy_to_schema(p: AuthPolicy) -> AuthPolicySchema:
    return AuthPolicySchema(
        id=p.id,
        group_id=p.group_id,
        group_name=p.group.name if p.group else None,
        requirement=p.requirement,
        updated_at=str(p.updated_at),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router_auth_policy.get('/', response=List[AuthPolicySchema])
def list_policies(request):
    """List the global default + all per-group policy overrides."""
    _require_superuser(request)
    return [_policy_to_schema(p) for p in AuthPolicy.objects.select_related('group').all()]


@router_auth_policy.post('/', response=AuthPolicySchema)
def create_policy(request, payload: AuthPolicyCreateSchema):
    """Create a per-group policy override (or the global default)."""
    _require_superuser(request)
    if payload.requirement not in AuthPolicy.Requirement.values:
        raise HttpError(400, f'Invalid requirement: {payload.requirement}')
    group = None
    if payload.group_id:
        group = get_object_or_404(Group, pk=payload.group_id)
        if AuthPolicy.objects.filter(group=group).exists():
            raise HttpError(400, f'Policy already exists for group "{group.name}". Edit it instead.')
    else:
        if AuthPolicy.objects.filter(group__isnull=True).exists():
            raise HttpError(400, 'Global default policy already exists. Edit it instead.')
    policy = AuthPolicy.objects.create(
        group=group,
        requirement=payload.requirement,
        updated_by=request.user,
    )
    return _policy_to_schema(policy)


@router_auth_policy.get('/affected-count/', response=AffectedCountSchema)
def affected_count(request, requirement: str, group_id: Optional[int] = None):
    """How many non-superuser active users would be affected."""
    _require_superuser(request)
    if group_id:
        count = User.objects.filter(
            is_active=True, is_superuser=False, groups__id=group_id,
        ).count()
    else:
        count = User.objects.filter(is_active=True, is_superuser=False).count()
    return AffectedCountSchema(count=count)


@router_auth_policy.get('/groups/', response=List[GroupListSchema])
def list_groups(request):
    """List all Django groups (for policy override UI)."""
    _require_superuser(request)
    return [
        GroupListSchema(id=g.id, name=g.name, user_count=g.user_set.count())
        for g in Group.objects.all()
    ]


@router_auth_policy.get('/user/{int:user_id}/', response=UserPolicySchema)
def user_effective_policy(request, user_id: int):
    """What effective policy applies to a specific user."""
    _require_superuser(request)
    target = get_object_or_404(User, pk=user_id)
    return UserPolicySchema(
        user_id=target.id,
        username=target.username,
        effective_policy=resolve_policy(target),
    )


@router_auth_policy.put('/{int:policy_id}/', response=AuthPolicySchema)
def update_policy(request, policy_id: int, payload: AuthPolicyUpdateSchema):
    """Update the requirement on an existing policy row."""
    _require_superuser(request)
    policy = get_object_or_404(AuthPolicy, pk=policy_id)
    if payload.requirement not in AuthPolicy.Requirement.values:
        raise HttpError(400, f'Invalid requirement: {payload.requirement}')
    # Step-up confirmation for SSO_REQUIRED
    if payload.requirement == AuthPolicy.Requirement.SSO_REQUIRED:
        if not payload.confirm_password or not request.user.check_password(payload.confirm_password):
            raise HttpError(403, 'Password confirmation required for SSO-only policy.')
    policy.requirement = payload.requirement
    policy.updated_by = request.user
    policy.save()
    return _policy_to_schema(policy)


@router_auth_policy.delete('/{int:policy_id}/')
def delete_policy(request, policy_id: int):
    """Delete a per-group override (falls back to global default)."""
    _require_superuser(request)
    policy = get_object_or_404(AuthPolicy, pk=policy_id)
    policy.delete()
    return {'ok': True}
