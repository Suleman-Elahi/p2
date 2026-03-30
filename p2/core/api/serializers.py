"""p2 core serializers"""
from rest_framework.serializers import ReadOnlyField, Serializer

from p2.api.serializers import TagModelSerializer
from p2.core.models import Storage, Volume


class BlobPayloadSerializer(Serializer):
    """Blob payload serializer"""

    payload = ReadOnlyField()

    def create(self):
        raise NotImplementedError()

    def update(self):
        raise NotImplementedError()


class VolumeSerializer(TagModelSerializer):
    """Volume Serializer"""

    class Meta:

        model = Volume
        fields = ['uuid', 'name', 'storage', 'tags', 'predefined_keys']


class StorageSerializer(TagModelSerializer):
    """Storage Serializer"""

    provider = ReadOnlyField()

    class Meta:

        model = Storage
        fields = ['uuid', 'name', 'controller', 'tags', 'predefined_keys', 'provider']
