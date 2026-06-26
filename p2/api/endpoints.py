"""p2 API Ninja Endpoints (System/Auth)"""
from typing import List
from django.shortcuts import get_object_or_404
from django.contrib.auth.models import User
from ninja import Router
from p2.api.models import APIKey
from p2.api.schemas import APIKeySchema, APIKeyCreateSchema, UserSchema, UserCreateSchema
from p2.lib.config import CONFIG

router_user = Router(tags=["system-user"])
router_key = Router(tags=["system-key"])
router_config = Router(tags=["system-config"])

@router_user.get("/", response=List[UserSchema])
def list_users(request):
    return User.objects.all()

@router_user.get("/{user_id}/", response=UserSchema)
def get_user(request, user_id: int):
    return get_object_or_404(User, id=user_id)

@router_user.post("/", response=UserSchema)
def create_user(request, payload: UserCreateSchema):
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
    return APIKey.objects.all()

@router_key.post("/", response=APIKeySchema)
def create_key(request, payload: APIKeyCreateSchema):
    key = APIKey.objects.create(
        name=payload.name,
        user=request.user,
        access_key=payload.access_key if payload.access_key else None,
    )
    return key

@router_key.get("/{key_id}/", response=APIKeySchema)
def get_key(request, key_id: int):
    return get_object_or_404(APIKey, id=key_id)

@router_key.delete("/{key_id}/")
def delete_key(request, key_id: int):
    key = get_object_or_404(APIKey, id=key_id)
    key.delete()
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
