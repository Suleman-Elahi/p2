"""UI views for browsing blobs via LSM"""
import json
import logging
import os
import zipfile
import queue
import threading

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, FileResponse, StreamingHttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from asgiref.sync import sync_to_async

from p2.core.acl import has_volume_permission
from p2.core.constants import ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME
from p2.core.models import Volume
from p2.s3.engine import get_engine as _get_engine

LOGGER = logging.getLogger(__name__)

CHUNK = 64 * 1024


async def _require_login(request):
    """Return True if authenticated, False otherwise (for async views)."""
    is_auth = await sync_to_async(lambda: request.user.is_authenticated)()
    return is_auth


def _login_redirect(request):
    """Return a redirect response to the login page with next= set correctly."""
    login_url = reverse('auth_login')
    return redirect(f"{login_url}?next={request.path}")


class BlobPseudo:
    def __init__(self, volume, path, attributes):
        self.volume = volume
        self.path = path
        self.attributes = attributes
        parts = path.strip('/').split('/')
        self.filename = parts[-1] if parts else ''
        self.tags = {}


class BlobListView(View):
    async def get(self, request, volume_pk):
        if not await _require_login(request):
            return _login_redirect(request)
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
                breadcrumbs.append({'full': current, 'part': part})

        engine = await sync_to_async(_get_engine)(volume)
        try:
            items = list(engine.list(prefix))
        except Exception as e:
            LOGGER.error("Failed to list: %s", e)
            items = []

        prefixes = set()
        objects = []
        folder_stats: dict = {}

        for key, json_val in items:
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix):]
            if not remainder:
                continue
            slash_idx = remainder.find('/')
            if slash_idx != -1:
                folder_key = prefix + remainder[:slash_idx + 1]
                prefixes.add(folder_key)
                try:
                    attr = json.loads(json_val)
                    if not attr.get(ATTR_BLOB_IS_FOLDER, False):
                        stats = folder_stats.setdefault(folder_key, {'count': 0, 'bytes': 0})
                        stats['count'] += 1
                        stats['bytes'] += int(attr.get(ATTR_BLOB_SIZE_BYTES, 0) or 0)
                except Exception:
                    pass
            else:
                try:
                    attr = json.loads(json_val)
                    if attr.get(ATTR_BLOB_IS_FOLDER, False):
                        prefixes.add(key)
                    else:
                        objects.append(BlobPseudo(volume, key, attr))
                except Exception:
                    pass

        prefix_objs = [
            {
                'absolute_path': p,
                'relative_path': p[len(prefix):].rstrip('/'),
                'count': folder_stats.get(p, {}).get('count', 0),
                'bytes': folder_stats.get(p, {}).get('bytes', 0),
            }
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


class BlobDetailView(View):
    async def get(self, request, volume_pk, blob_path):
        if not await _require_login(request):
            return _login_redirect(request)
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
            breadcrumbs.append({'full': current, 'part': part})

        context = {
            'volume': volume,
            'object': blob,
            'breadcrumbs': breadcrumbs,
            'permissions': [],
        }
        return await sync_to_async(render)(request, 'p2_core/blob_detail.html', context)


class BlobInlineView(View):
    """Serve blob inline for preview."""
    async def get(self, request, volume_pk, blob_path):
        if not await _require_login(request):
            return _login_redirect(request)
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'read'):
            raise PermissionDenied
        engine = await sync_to_async(_get_engine)(volume)
        metadata_json = engine.get(blob_path)
        if not metadata_json:
            raise Http404
        attributes = json.loads(metadata_json)
        internal_path = attributes.get('internal_path')
        if not internal_path:
            raise Http404
        from p2.core.storage_path import internal_to_fs
        fs_path = internal_to_fs(internal_path)
        mime = attributes.get(ATTR_BLOB_MIME, 'application/octet-stream')
        try:
            response = FileResponse(open(fs_path, 'rb'), content_type=mime)
            response['X-Frame-Options'] = 'SAMEORIGIN'
            return response
        except Exception:
            raise Http404


class BlobDownloadView(View):
    async def get(self, request, volume_pk, blob_path):
        if not await _require_login(request):
            return _login_redirect(request)
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'read'):
            raise PermissionDenied
        engine = await sync_to_async(_get_engine)(volume)
        metadata_json = engine.get(blob_path)
        if not metadata_json:
            raise Http404
        attributes = json.loads(metadata_json)
        internal_path = attributes.get('internal_path')
        if not internal_path:
            raise Http404
        from p2.core.storage_path import internal_to_fs
        fs_path = internal_to_fs(internal_path)
        try:
            return FileResponse(open(fs_path, 'rb'), as_attachment=True,
                                filename=blob_path.split('/')[-1])
        except Exception:
            raise Http404


class BlobDeleteView(View):
    async def post(self, request, volume_pk, blob_path):
        return await self._delete(request, volume_pk, blob_path)

    async def get(self, request, volume_pk, blob_path):
        return await self._delete(request, volume_pk, blob_path)

    async def _delete(self, request, volume_pk, blob_path):
        if not await _require_login(request):
            return _login_redirect(request)
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'delete'):
            raise PermissionDenied
        engine = await sync_to_async(_get_engine)(volume)
        metadata_json = await sync_to_async(engine.get)(blob_path)
        if metadata_json:
            attributes = json.loads(metadata_json)
            internal_path = attributes.get('internal_path')
            if internal_path:
                from p2.core.storage_path import internal_to_fs
                fs_path = internal_to_fs(internal_path)
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
            await sync_to_async(engine.delete)(blob_path)
        prefix = '/'.join(blob_path.split('/')[:-1])
        qs = f'?prefix={prefix}/' if prefix else ''
        return redirect(
            reverse('p2_ui:core-blob-list', kwargs={'volume_pk': volume_pk}) + qs
        )


class FolderDownloadView(View):
    async def get(self, request, volume_pk, folder_prefix):
        if not await _require_login(request):
            return _login_redirect(request)
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'read'):
            raise PermissionDenied

        prefix = folder_prefix.strip('/') + '/'
        engine = await sync_to_async(_get_engine)(volume)
        try:
            items = await sync_to_async(list)(engine.list(prefix))
        except Exception as e:
            LOGGER.error("Failed to list for ZIP: %s", e)
            items = []

        blobs = []
        for key, json_val in items:
            if not key.startswith(prefix):
                continue
            try:
                attr = json.loads(json_val)
                if not attr.get(ATTR_BLOB_IS_FOLDER, False):
                    internal_path = attr.get('internal_path', '')
                    from p2.core.storage_path import internal_to_fs
                    fs_path = internal_to_fs(internal_path) if internal_path else ''
                    blobs.append((key, fs_path))
            except Exception:
                pass

        def _zip_generator():
            q = queue.Queue(maxsize=16)
            SENTINEL = object()

            class _StreamIO:
                def __init__(self):
                    self._pos = 0
                def write(self, data):
                    if data:
                        q.put(bytes(data))
                    self._pos += len(data)
                    return len(data)
                def flush(self): pass
                def tell(self): return self._pos

            def _worker():
                try:
                    stream = _StreamIO()
                    with zipfile.ZipFile(stream, mode='w', allowZip64=True) as zf:
                        for key, fs_path in blobs:
                            arcname = key[len(prefix):]
                            zi = zipfile.ZipInfo(arcname)
                            zi.compress_type = zipfile.ZIP_DEFLATED
                            try:
                                with zf.open(zi, 'w', force_zip64=True) as dest:
                                    with open(fs_path, 'rb') as src:
                                        while True:
                                            chunk = src.read(CHUNK)
                                            if not chunk:
                                                break
                                            dest.write(chunk)
                            except Exception as e:
                                LOGGER.warning("Skipping %s in ZIP: %s", key, e)
                except Exception as e:
                    LOGGER.error("ZIP error: %s", e)
                finally:
                    q.put(SENTINEL)

            threading.Thread(target=_worker, daemon=True).start()
            while True:
                item = q.get()
                if item is SENTINEL:
                    break
                yield item

        folder_name = prefix.strip('/').split('/')[-1] or volume.name
        response = StreamingHttpResponse(_zip_generator(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{folder_name}.zip"'
        return response


class FolderDeleteView(View):
    async def post(self, request, volume_pk, folder_prefix):
        return await self.get(request, volume_pk, folder_prefix)

    async def get(self, request, volume_pk, folder_prefix):
        if not await _require_login(request):
            return _login_redirect(request)
        volume = await sync_to_async(get_object_or_404)(Volume, pk=volume_pk)
        if not await has_volume_permission(request.user, volume, 'delete'):
            raise PermissionDenied

        prefix = folder_prefix.strip('/') + '/'
        engine = await sync_to_async(_get_engine)(volume)
        try:
            items = await sync_to_async(list)(engine.list(prefix))
        except Exception as e:
            LOGGER.error("Failed to list for delete: %s", e)
            items = []

        for key, json_val in items:
            if not key.startswith(prefix):
                continue
            try:
                attr = json.loads(json_val)
                internal_path = attr.get('internal_path', '')
                if internal_path:
                    from p2.core.storage_path import internal_to_fs
                    fs_path = internal_to_fs(internal_path)
                    try:
                        os.remove(fs_path)
                    except OSError:
                        pass
                await sync_to_async(engine.delete)(key)
            except Exception as e:
                LOGGER.warning("Error deleting %s: %s", key, e)

        parent = '/'.join(prefix.strip('/').split('/')[:-1])
        parent_qs = ('?prefix=' + parent + '/') if parent else ''
        return redirect(
            reverse('p2_ui:core-blob-list', kwargs={'volume_pk': volume_pk}) + parent_qs
        )
