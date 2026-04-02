"""Core API Viewsets."""
import hashlib
import json
import logging
import os
import uuid

from asgiref.sync import async_to_sync
from django.utils.timezone import now
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from p2.core.acl import has_volume_permission
from p2.core.api.serializers import StorageSerializer, VolumeSerializer
from p2.core.constants import (ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME,
                                ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_STAT_CTIME,
                                ATTR_BLOB_STAT_MTIME)
from p2.core.models import Storage, Volume
from p2.s3.engine import get_engine as _get_engine

LOGGER = logging.getLogger(__name__)


def _check_permission(user, volume, permission):
    return async_to_sync(has_volume_permission)(user, volume, permission)


class VolumeViewSet(ModelViewSet):
    """List of all Volumes a user can see"""
    queryset = Volume.objects.all()
    serializer_class = VolumeSerializer

    @action(detail=True, methods=['post'])
    def upload(self, request, pk=None):
        """Direct multipart upload — streams file to disk, writes metadata to redb."""
        volume = self.get_object()
        if not _check_permission(request.user, volume, 'write'):
            raise PermissionDenied("No write permission on this volume")

        prefix = request.query_params.get('prefix', '').strip('/')
        uploaded = []

        for uploaded_file in request.FILES.getlist('file'):
            rel_path = request.POST.get('relativePath', uploaded_file.name)
            key = f"{prefix}/{rel_path.lstrip('/')}" if prefix else rel_path.lstrip('/')

            blob_uuid = uuid.uuid4().hex
            dir_path = os.path.join("/storage/volumes", volume.uuid.hex,
                                    blob_uuid[0:2], blob_uuid[2:4])
            os.makedirs(dir_path, exist_ok=True)
            fs_path = os.path.join(dir_path, blob_uuid)
            internal_path = (
                f"/internal-storage/volumes/{volume.uuid.hex}"
                f"/{blob_uuid[0:2]}/{blob_uuid[2:4]}/{blob_uuid}"
            )

            md5_hash = hashlib.md5()
            blob_size = 0
            with open(fs_path, 'wb') as f:
                for chunk in uploaded_file.chunks(chunk_size=1 << 20):
                    f.write(chunk)
                    md5_hash.update(chunk)
                    blob_size += len(chunk)

            final_md5 = md5_hash.hexdigest()
            engine = _get_engine(volume)
            existing_json = engine.get(key)
            attrs = json.loads(existing_json) if existing_json else {}
            attrs.update({
                ATTR_BLOB_MIME: uploaded_file.content_type or 'application/octet-stream',
                ATTR_BLOB_SIZE_BYTES: str(blob_size),
                ATTR_BLOB_IS_FOLDER: False,
                ATTR_BLOB_STAT_MTIME: str(now()),
                'blob.p2.io/hash/md5': final_md5,
                'internal_path': internal_path,
            })
            if not existing_json:
                attrs[ATTR_BLOB_STAT_CTIME] = str(now())
            engine.put(key, json.dumps(attrs))

            try:
                from p2.core.events import STREAM_BLOB_POST_SAVE, make_event, publish_event
                event = make_event(
                    blob_uuid=blob_uuid,
                    volume_uuid=volume.uuid.hex,
                    event_type="blob_post_save",
                )
                event['blob_path'] = key
                event['mime'] = uploaded_file.content_type or 'application/octet-stream'
                async_to_sync(publish_event)(STREAM_BLOB_POST_SAVE, event)
            except Exception as exc:
                LOGGER.warning("Failed to publish blob event: %s", exc)

            uploaded.append({'path': key, 'size': blob_size, 'etag': final_md5})

        return Response({'uploaded': uploaded})

    @action(detail=True, methods=['post'])
    def re_index(self, request, pk=None):
        return Response(0)


class StorageViewSet(ModelViewSet):
    """List of all Storages"""
    queryset = Storage.objects.all()
    serializer_class = StorageSerializer
