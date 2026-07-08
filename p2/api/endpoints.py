"""p2 API Ninja Endpoints (System/Auth)"""
from typing import List
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.contrib.auth.models import User
from ninja import Router
from p2.api.models import APIKey
from p2.api.schemas import (
    APIKeyCreatedSchema,
    APIKeyCreateSchema,
    APIKeySchema,
    UserCreateSchema,
    UserSchema,
)
from p2.s3.cache import invalidate_apikey
from p2.lib.config import CONFIG

router_user = Router(tags=["system-user"])
router_key = Router(tags=["system-key"])
router_config = Router(tags=["system-config"])


def _require_superuser(request):
    if not getattr(request.user, 'is_authenticated', False) or not getattr(request.user, 'is_superuser', False):
        raise PermissionDenied("Superuser privileges required.")


def _key_queryset_for_user(request):
    if getattr(request.user, 'is_superuser', False):
        return APIKey.objects.all()
    return APIKey.objects.filter(user=request.user)


@router_user.get("/", response=List[UserSchema])
def list_users(request):
    _require_superuser(request)
    return User.objects.all()

@router_user.get("/{user_id}/", response=UserSchema)
def get_user(request, user_id: int):
    _require_superuser(request)
    return get_object_or_404(User, id=user_id)

@router_user.post("/", response=UserSchema)
def create_user(request, payload: UserCreateSchema):
    _require_superuser(request)
    user = User.objects.create_user(
        username=payload.username,
        password=payload.password,
        email=payload.email,
    )
    if payload.is_superuser:
        user.is_superuser = True
        user.is_staff = True
        user.save()
    return user

@router_key.get("/", response=List[APIKeySchema])
def list_keys(request):
    return _key_queryset_for_user(request)

@router_key.post("/", response=APIKeyCreatedSchema)
def create_key(request, payload: APIKeyCreateSchema):
    owner = request.user
    if payload.user:
        _require_superuser(request)
        owner = get_object_or_404(User, id=payload.user)

    key_kwargs = {
        'name': payload.name,
        'user': owner,
    }
    if payload.access_key:
        key_kwargs['access_key'] = payload.access_key
    key = APIKey.objects.create(**key_kwargs)
    return key

@router_key.get("/{key_id}/", response=APIKeySchema)
def get_key(request, key_id: int):
    return get_object_or_404(_key_queryset_for_user(request), id=key_id)

@router_key.delete("/{key_id}/")
def delete_key(request, key_id: int):
    key = get_object_or_404(_key_queryset_for_user(request), id=key_id)
    access_key = key.access_key
    key.delete()
    invalidate_apikey(access_key)
    return {"success": True}

# ── System Config ──────────────────────────────────────────────────────────

STORAGE_CLASSES = [
    {"value": "STANDARD", "label": "Standard"},
]

ENCRYPTION_OPTIONS = [
    {"value": "AES-256", "label": "SSE-S3 (AES-256)"},
    {"value": "aws:kms", "label": "SSE-KMS (aws:kms)"},
    {"value": "none", "label": "None"},
]

REGION_OPTIONS = [
    {"value": "us-east-1", "label": "US East (N. Virginia)"},
    {"value": "us-west-2", "label": "US West (Oregon)"},
    {"value": "eu-west-1", "label": "EU (Ireland)"},
    {"value": "eu-central-1", "label": "EU (Frankfurt)"},
    {"value": "ap-southeast-1", "label": "Asia Pacific (Singapore)"},
    {"value": "ap-northeast-1", "label": "Asia Pacific (Tokyo)"},
]

@router_config.get("/")
def get_config(request):
    # Use the request's Host header as fallback so the UI shows the
    # actual endpoint rather than a placeholder from config.
    s3_endpoint = CONFIG.y("s3.base_domain", None)
    if not s3_endpoint or s3_endpoint == "s3.example.com":
        s3_endpoint = request.get_host()
    return {
        "s3_endpoint": s3_endpoint,
        "storage_classes": STORAGE_CLASSES,
        "encryption_options": ENCRYPTION_OPTIONS,
        "region_options": REGION_OPTIONS,
        "version": "1.0.0",
    }
