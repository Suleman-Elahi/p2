"""UI views for browsing blobs via LSM"""
import json
import logging
import os
from django.shortcuts import render, get_object_or_404, redirect
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, FileResponse
from asgiref.sync import sync_to_async

from p2.core.models import Volume
from p2.core.acl import has_volume_permission
from p2.s3.engine import get_engine as _get_engine
from p2.core.constants import ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME

LOGGER = logging.getLogger(__name__)


class BlobPseudo:
    def __init__(self, volume, path, attributes):
        self.volume = volume
        self.path = path
        self.attributes = attributes
        parts = path.strip('/').split('/')
        self.filename = parts[-1] if parts else ''
        self.tags = {}

    def read(self):
        internal_path = self.attributes.get('internal_path')
        if not internal_path:
            return b""
        fs_path = internal_path.replace('/internal-storage/', '/storage/')
        try:
            with open(fs_path, 'rb') as f:
                return f.read()
        except Exception:
            return b""


class BlobListView(LoginRequiredMixin, View):
    async def get(self, request, volume_pk):
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'list'):
            raise PermissionDenied

        prefix = request.GET.get('prefix', '')
        if prefix and not prefix.endswith('/'):
            prefix += '/'

        breadcrumbs = []
        if prefix:
            parts = [p for p in prefix.split('/') if p]
            current = ''
            for part in parts:
                current += part + '/'
                breadcrumbs.append({'full': current, 'prefix': current, 'part': part, 'title': part})

        engine = await sync_to_async(_get_engine)(volume)
        try:
            items = engine.list(prefix)
        except Exception as e:
            LOGGER.error("Failed to list: %s", e)
            items = []

        prefixes = set()
        objects = []

        for key, json_val in items:
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix):]
            if not remainder and key == prefix:
        for key, json_val in items:
            # Normalize key: strip leading slash so listing works with prefix=''
            norm_key = key.lstrip('/')
            if not norm_key.startswith(prefix):
                continue
            remainder = norm_key[len(prefix):]
            if not remainder and norm_key == prefix:
            if slash_idx != -1:
                folder_name = remainder[:slash_idx + 1]
                prefixes.add(prefix + folder_name)
            else:
                try:
                    attr = json.loads(json_val)
                    if attr.get(ATTR_BLOB_IS_FOLDER, False):
                        prefixes.add(norm_key)
                    else:
                        objects.append(BlobPseudo(volume, norm_key, attr))
        prefix_objs = [
            {'absolute_path': p, 'relative_path': p[len(prefix):].rstrip('/')}
            for p in sorted(prefixes)
        ]
        objects.sort(key=lambda x: x.filename)

        context = {
            'volume': volume,
            'breadcrumbs': breadcrumbs,
            'prefixes': prefix_objs,
            'object_list': objects,
            'is_paginated': False,
        }
        return await sync_to_async(render)(request, 'p2_core/blob_list.html', context)


class BlobDetailView(LoginRequiredMixin, View):
    async def get(self, request, volume_pk, blob_path):
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'read'):
            raise PermissionDenied

        engine = await sync_to_async(_get_engine)(volume)
        metadata_json = engine.get(blob_path)
        if not metadata_json:
            raise Http404

        attributes = json.loads(metadata_json)
        blob = BlobPseudo(volume, blob_path, attributes)

        from p2.s3.constants import TAG_S3_USER_TAG_PREFIX
        blob.tags = {
            k[len(TAG_S3_USER_TAG_PREFIX):]: v
            for k, v in attributes.items()
            if k.startswith(TAG_S3_USER_TAG_PREFIX)
        }

        parts = [p for p in blob_path.split('/') if p]
        breadcrumbs = []
        current = ''
        for part in parts[:-1]:
            current += part + '/'
            breadcrumbs.append({'full': current, 'prefix': current, 'part': part, 'title': part})

        users_perms = {request.user.username: ['read', 'write', 'delete']}
        if await has_volume_permission(request.user, volume, 'admin'):
            users_perms[request.user.username].append('admin')

        context = {
            'volume': volume,
            'object': blob,
            'breadcrumbs': breadcrumbs,
            'users_perms': users_perms,
            'model_perms': [
                {'name': 'Read', 'codename': 'read'},
                {'name': 'Write', 'codename': 'write'},
                {'name': 'Delete', 'codename': 'delete'},
            ],
            'permissions': [],
        }
        return await sync_to_async(render)(request, 'p2_core/blob_detail.html', context)


class BlobDownloadView(LoginRequiredMixin, View):
    async def get(self, request, volume_pk, blob_path):
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'read'):
            raise PermissionDenied
class BlobInlineView(LoginRequiredMixin, View):
    """Serve a blob inline (for preview) — no Content-Disposition: attachment."""
    def get(self, request, volume_pk, blob_path):
        volume = get_object_or_404(Volume, pk=volume_pk)
        if not _check_permission(request.user, volume, 'read'):
            raise PermissionDenied

        engine = _get_engine(volume)
        metadata_json = engine.get(blob_path)
        if not metadata_json:
            raise Http404

        attributes = json.loads(metadata_json)
        internal_path = attributes.get('internal_path')
        if not internal_path:
            raise Http404

        fs_path = internal_path.replace('/internal-storage/', '/storage/')
        mime = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        try:
            return FileResponse(open(fs_path, 'rb'), content_type=mime)
        except Exception:
            raise Http404


class BlobDownloadView(LoginRequiredMixin, View):
    def get(self, request, volume_pk, blob_path):
        if not metadata_json:
            raise Http404

        attributes = json.loads(metadata_json)
        internal_path = attributes.get('internal_path')
        if not internal_path:
            raise Http404

        fs_path = internal_path.replace('/internal-storage/', '/storage/')
        try:
            return FileResponse(open(fs_path, 'rb'), as_attachment=True, filename=blob_path.split('/')[-1])
        except Exception:
            raise Http404


class BlobDeleteView(LoginRequiredMixin, View):
    async def get(self, request, volume_pk, blob_path):
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'delete'):
            raise PermissionDenied

        engine = await sync_to_async(_get_engine)(volume)
        metadata_json = engine.get(blob_path)
        if not metadata_json:
            raise Http404

        attributes = json.loads(metadata_json)
        internal_path = attributes.get('internal_path')
        if internal_path:
            fs_path = internal_path.replace('/internal-storage/', '/storage/')
            try:
                os.remove(fs_path)
            except OSError:
                pass

        engine.delete(blob_path)
        return await sync_to_async(redirect)('p2_ui:core-blob-list', volume_pk=volume_pk)
