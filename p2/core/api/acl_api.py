"""Bucket VolumeACL management API (Django Ninja).

Provides CRUD on VolumeACL entries for bucket admins and superusers.
"""
import logging
from typing import List, Optional

from asgiref.sync import async_to_sync
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError

from p2.core.acl import VolumeACL, has_volume_permission
from p2.core.models import Volume
from p2.s3.cache import invalidate_acl

LOGGER = logging.getLogger(__name__)

router_acl = Router(tags=["core-acl"])


class VolumeACLSchema(Schema):
    id: int
    volume_uuid: str
    user_id: Optional[int] = None
    username: Optional[str] = None
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    permissions: List[str]


class VolumeACLCreateSchema(Schema):
    user_id: Optional[int] = None
    group_id: Optional[int] = None
    permissions: List[str]  # e.g. ["read", "write", "delete", "list", "admin"]


class GranteeUserSchema(Schema):
    id: int
    username: str
    email: Optional[str] = None


class GranteeGroupSchema(Schema):
    id: int
    name: str
    user_count: int


class GrantablesSchema(Schema):
    users: List[GranteeUserSchema]
    groups: List[GranteeGroupSchema]


def _require_bucket_admin(user, volume: Volume):
    if not async_to_sync(has_volume_permission)(user, volume, "admin"):
        raise PermissionDenied("You do not have 'admin' permission on this bucket.")


def _acl_to_schema(acl: VolumeACL) -> VolumeACLSchema:
    perms = acl.permissions if isinstance(acl.permissions, list) else [p.strip() for p in str(acl.permissions).split(",") if p.strip()]
    return VolumeACLSchema(
        id=acl.id,
        volume_uuid=str(acl.volume.uuid),
        user_id=acl.user_id,
        username=acl.user.username if acl.user else None,
        group_id=acl.group_id,
        group_name=acl.group.name if acl.group else None,
        permissions=perms,
    )


@router_acl.get("/volumes/{volume_uuid}/acl/", response=List[VolumeACLSchema])
@router_acl.get("/volume/{volume_uuid}/acl/", response=List[VolumeACLSchema])
def list_acl(request, volume_uuid: str):
    vol = get_object_or_404(Volume, uuid=volume_uuid)
    _require_bucket_admin(request.user, vol)
    acls = VolumeACL.objects.filter(volume=vol).select_related("user", "group")
    return [_acl_to_schema(a) for a in acls]


@router_acl.get("/volumes/{volume_uuid}/acl/grantables/", response=GrantablesSchema)
@router_acl.get("/volume/{volume_uuid}/acl/grantables/", response=GrantablesSchema)
def list_grantables(request, volume_uuid: str):
    """Users and groups a bucket admin can grant access to.

    The generic /system/user and auth-policy group endpoints are
    superuser-only, so a non-superuser bucket owner could not populate the
    assignment dialog. This endpoint scopes the listing to bucket admins.
    """
    vol = get_object_or_404(Volume, uuid=volume_uuid)
    _require_bucket_admin(request.user, vol)

    users = User.objects.filter(is_active=True).order_by("username")
    groups = Group.objects.all().order_by("name")
    return GrantablesSchema(
        users=[
            GranteeUserSchema(id=u.id, username=u.username, email=u.email or "")
            for u in users
        ],
        groups=[
            GranteeGroupSchema(id=g.id, name=g.name, user_count=g.user_set.count())
            for g in groups
        ],
    )


@router_acl.post("/volumes/{volume_uuid}/acl/", response=VolumeACLSchema)
@router_acl.post("/volume/{volume_uuid}/acl/", response=VolumeACLSchema)
def grant_acl(request, volume_uuid: str, payload: VolumeACLCreateSchema):
    vol = get_object_or_404(Volume, uuid=volume_uuid)
    _require_bucket_admin(request.user, vol)

    if (payload.user_id is None and payload.group_id is None) or (payload.user_id is not None and payload.group_id is not None):
        raise HttpError(400, "Specify exactly one of user_id or group_id.")

    user = None
    group = None
    if payload.user_id is not None:
        user = get_object_or_404(User, id=payload.user_id)
    if payload.group_id is not None:
        group = get_object_or_404(Group, id=payload.group_id)

    # Validate permission names
    valid_perms = {"read", "write", "delete", "list", "admin"}
    perms = [p.lower().strip() for p in payload.permissions if p.lower().strip() in valid_perms]
    if not perms:
        raise HttpError(400, "At least one valid permission required (read, write, delete, list, admin).")

    acl, _ = VolumeACL.objects.update_or_create(
        volume=vol,
        user=user,
        group=group,
        defaults={"permissions": perms},
    )
    invalidate_acl(str(vol.pk))
    return _acl_to_schema(acl)


@router_acl.delete("/volumes/{volume_uuid}/acl/{acl_id}/")
@router_acl.delete("/volume/{volume_uuid}/acl/{acl_id}/")
def revoke_acl(request, volume_uuid: str, acl_id: int):
    vol = get_object_or_404(Volume, uuid=volume_uuid)
    _require_bucket_admin(request.user, vol)
    acl = get_object_or_404(VolumeACL, id=acl_id, volume=vol)
    acl.delete()
    invalidate_acl(str(vol.pk))
    return {"ok": True}
