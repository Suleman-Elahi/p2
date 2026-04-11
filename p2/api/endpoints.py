"""p2 API Ninja Endpoints (System/Auth)"""
from typing import List
from django.shortcuts import get_object_or_404
from django.contrib.auth.models import User
from ninja import Router
from p2.api.models import APIKey
from p2.api.schemas import APIKeySchema, APIKeyCreateSchema, UserSchema

router_user = Router(tags=["system-user"])
router_key = Router(tags=["system-key"])

@router_user.get("/", response=List[UserSchema])
def list_users(request):
    return User.objects.all()

@router_user.get("/{user_id}/", response=UserSchema)
def get_user(request, user_id: int):
    return get_object_or_404(User, id=user_id)

@router_key.get("/", response=List[APIKeySchema])
def list_keys(request):
    return APIKey.objects.all()

@router_key.post("/", response=APIKeySchema)
def create_key(request, payload: APIKeyCreateSchema):
    key = APIKey.objects.create(**payload.dict(exclude_unset=True))
    return key

@router_key.get("/{key_id}/", response=APIKeySchema)
def get_key(request, key_id: int):
    return get_object_or_404(APIKey, id=key_id)

@router_key.delete("/{key_id}/")
def delete_key(request, key_id: int):
    key = get_object_or_404(APIKey, id=key_id)
    key.delete()
    return {"success": True}
