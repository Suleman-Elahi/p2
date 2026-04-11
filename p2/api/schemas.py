"""p2 API Schemas (Django Ninja)"""
from typing import Optional
from ninja import ModelSchema
from django.contrib.auth.models import User
from p2.api.models import APIKey

class UserSchema(ModelSchema):
    class Meta:
        model = User
        fields = "__all__"

class APIKeySchema(ModelSchema):
    secret_key: str

    @staticmethod
    def resolve_secret_key(obj):
        return obj.decrypt_secret_key()

    class Meta:
        model = APIKey
        fields = ['id', 'name', 'user', 'access_key']

class APIKeyCreateSchema(ModelSchema):
    class Meta:
        model = APIKey
        fields = ['name', 'user', 'access_key']
        optional_fields = ['access_key']
