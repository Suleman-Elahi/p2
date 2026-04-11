"""Core API Ninja Endpoints"""
import hashlib
import json
import logging
import os
import uuid
from typing import List

from asgiref.sync import async_to_sync
from django.utils.timezone import now
from django.shortcuts import get_object_or_404
from ninja import Router, File
from ninja.files import UploadedFile

from p2.core.acl import has_volume_permission
from p2.core.api.schemas import StorageSchema, VolumeSchema, UploadResponseSchema
from p2.core.constants import (ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME,
                                ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_STAT_CTIME,
                                ATTR_BLOB_STAT_MTIME)
from p2.core.models import Storage, Volume
from p2.s3.engine import get_engine as _get_engine

LOGGER = logging.getLogger(__name__)

router_volume = Router(tags=["core-volume"])
router_storage = Router(tags=["core-storage"])

def _check_permission(user, volume, permission):
    return async_to_sync(has_volume_permission)(user, volume, permission)

@router_volume.get("/", response=List[VolumeSchema])
def list_volumes(request):
    return Volume.objects.all()

@router_volume.get("/{volume_uuid}/", response=VolumeSchema)
def get_volume(request, volume_uuid: str):
    return get_object_or_404(Volume, uuid=volume_uuid)

@router_volume.post("/{volume_uuid}/upload/", response=UploadResponseSchema)
def upload_files(request, volume_uuid: str, prefix: str = "", file: List[UploadedFile] = File(...)):
    volume = get_object_or_404(Volume, uuid=volume_uuid)
    if not _check_permission(request.user, volume, 'write'):
        # Just return 403 standard, ninja handles exceptions if configured, or we can return custom.
        # But for now, we just raise standard exception which ninja catches.
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("No write permission on this volume")

    prefix = prefix.strip('/')
    uploaded_files = []

    for uploaded_file in file:
        # Django Ninja doesn't naturally parse 'relativePath' POST form field alongside multiple files easily
        # without Form models, but we can fall back to request.POST logic.
        rel_path = request.POST.get('relativePath', uploaded_file.name)
        key = f"{prefix}/{rel_path.lstrip('/')}" if prefix else rel_path.lstrip('/')

        blob_uuid = uuid.uuid4().hex
        from p2.core.storage_path import storage_path
        dir_path = storage_path("volumes", volume.uuid.hex, blob_uuid[0:2], blob_uuid[2:4])
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

        uploaded_files.append({'path': key, 'size': blob_size, 'etag': final_md5})

    return {"uploaded": uploaded_files}


@router_volume.post("/{volume_uuid}/re-index/")
def re_index(request, volume_uuid: str):
    volume = get_object_or_404(Volume, uuid=volume_uuid)
    if not _check_permission(request.user, volume, 'write'):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied("No write permission on this volume")
    return 0



@router_storage.get("/", response=List[StorageSchema])
def list_storages(request):
    return Storage.objects.all()

@router_storage.get("/{storage_uuid}/", response=StorageSchema)
def get_storage(request, storage_uuid: str):
    return get_object_or_404(Storage, uuid=storage_uuid)
