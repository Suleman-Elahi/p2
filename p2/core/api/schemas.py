"""p2 core api schemas (Django Ninja)"""
from ninja import ModelSchema, Schema
from p2.core.models import Storage, Volume
from typing import Dict, Any, Optional

class VolumeSchema(ModelSchema):
    predefined_keys: dict = {}
    
    class Meta:
        model = Volume
        fields = ['uuid', 'name', 'storage', 'tags']

class StorageSchema(ModelSchema):
    predefined_keys: dict = {}
    provider: str = ""
    
    class Meta:
        model = Storage
        fields = ['uuid', 'name', 'controller_path', 'tags']

class UploadResponseSchema(Schema):
    uploaded: list[dict]
