"""Core API Viewsets"""
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from p2.core.api.filters import BlobFilter
from p2.core.api.serializers import (BlobPayloadSerializer, BlobSerializer,
                                     StorageSerializer, VolumeSerializer)
from p2.core.constants import ATTR_BLOB_MIME
from p2.core.exceptions import BlobException
from p2.core.models import Blob, Storage, Volume
from p2.lib.shortcuts import get_object_for_user_or_404
from p2.lib.utils import b64encode


class BlobViewSet(ModelViewSet):
    """
    Viewset that only lists events if user has 'view' permissions, and only
    allows operations on individual events if user has appropriate 'view', 'add',
    'change' or 'delete' permissions.
    """
    queryset = Blob.objects.all()
    serializer_class = BlobSerializer
    filter_class = BlobFilter

    @action(detail=True, methods=['get'])
    # pylint: disable=invalid-name
    def payload(self, request, pk=None):
        """Return payload data as base64 string"""
        blob = self.get_object()
        return Response({
            'payload': 'data:%s;base64,%s' % (blob.attributes.get(ATTR_BLOB_MIME, 'text/plain'),
                                              b64encode(blob.read()).decode('utf-8'))
        })


class VolumeViewSet(ModelViewSet):
    """List of all Volumes a user can see"""
    queryset = Volume.objects.all()
    serializer_class = VolumeSerializer

    @action(detail=True, methods=['post'])
    # pylint: disable=invalid-name
    def upload(self, request, pk=None):
        """Create blob from HTML Form upload"""
        volume = get_object_for_user_or_404(request.user, 'p2_core.use_volume', pk=pk)
        blobs = []
        if not request.user.has_perm('p2_core.create_blob'):
            raise PermissionDenied()
        # If upload was made from a subdirectory, we accept the ?prefix parameter
        prefix = request.GET.get('prefix', '')
        for key in request.FILES:
            file = request.FILES[key]
            try:
                blob = Blob.from_uploaded_file(file, volume, prefix=prefix)
                blobs.append({'uuid': blob.uuid, 'path': blob.path, 'filename': blob.filename})
            except BlobException as exc:
                raise APIException(detail=repr(exc))
        return Response(blobs)

    @action(detail=True, methods=['post'])
    # pylint: disable=invalid-name
    def re_index(self, request, pk=None):
        """Re-index all blobs in a volume by publishing payload-updated events."""
        from p2.core.events import STREAM_BLOB_PAYLOAD_UPDATED, STREAM_BLOB_POST_SAVE, make_event
        from asgiref.sync import async_to_sync
        from p2.core.events import publish_event

        volume = get_object_for_user_or_404(request.user, 'p2_core.use_volume', pk=pk)
        _publish = async_to_sync(publish_event)
        count = 0
        for blob in volume.blob_set.all():
            _publish(STREAM_BLOB_PAYLOAD_UPDATED, make_event(
                blob_uuid=blob.uuid.hex,
                volume_uuid=volume.uuid.hex,
                event_type='blob_payload_updated',
            ))
            _publish(STREAM_BLOB_POST_SAVE, make_event(
                blob_uuid=blob.uuid.hex,
                volume_uuid=volume.uuid.hex,
                event_type='blob_post_save',
            ))
            count += 1
        return Response(count)


class StorageViewSet(ModelViewSet):
    """
    Viewset that only lists events if user has 'view' permissions, and only
    allows operations on individual events if user has appropriate 'view', 'add',
    'change' or 'delete' permissions.
    """
    queryset = Storage.objects.all()
    serializer_class = StorageSerializer
