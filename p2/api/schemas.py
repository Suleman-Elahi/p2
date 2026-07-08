"""p2 API Schemas (Django Ninja)"""
from typing import Optional
from ninja import ModelSchema, Schema
from django.contrib.auth.models import User
from p2.api.models import APIKey

class UserSchema(ModelSchema):
    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'is_active',
            'is_staff',
            'is_superuser',
            'date_joined',
            'last_login',
        ]

class UserCreateSchema(Schema):
    username: str
    password: str
    email: str = ""
    is_superuser: bool = False

class APIKeySchema(ModelSchema):
    class Meta:
        model = APIKey
        fields = ['id', 'name', 'user', 'access_key']


class APIKeyCreatedSchema(APIKeySchema):
    secret_key: str

    @staticmethod
    def resolve_secret_key(obj):
        return obj.decrypt_secret_key()

class APIKeyCreateSchema(ModelSchema):
    user: Optional[int] = None  # auto-set from request.user

    class Meta:
        model = APIKey
        fields = ['name', 'user', 'access_key']
        optional_fields = ['access_key', 'user']
