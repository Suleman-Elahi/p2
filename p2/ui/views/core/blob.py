"""UI views for browsing blobs via LSM"""
import json
import logging
import os
from asgiref.sync import async_to_sync
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, FileResponse, StreamingHttpResponse

from p2.core.models import Volume
from p2.core.acl import has_volume_permission
from p2.s3.engine import get_engine as _get_engine
from p2.core.constants import ATTR_BLOB_SIZE_BYTES, ATTR_BLOB_IS_FOLDER, ATTR_BLOB_MIME

LOGGER = logging.getLogger(__name__)




def _check_permission(user, volume, permission):
    """Sync wrapper for async has_volume_permission."""
    return async_to_sync(has_volume_permission)(user, volume, permission)


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
    def get(self, request, volume_pk):
        volume = get_object_or_404(Volume, pk=volume_pk)
        if not _check_permission(request.user, volume, 'list'):
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

        engine = _get_engine(volume)
        try:
            items = engine.list(prefix)
        except Exception as e:
            LOGGER.error("Failed to list: %s", e)
            items = []

        prefixes = set()
        objects = []
        # folder_stats: prefix -> {'count': int, 'bytes': int}
        folder_stats: dict = {}

        for key, json_val in items:
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix):]
            if not remainder and key == prefix:
                continue
            slash_idx = remainder.find('/')
            if slash_idx != -1:
                folder_name = remainder[:slash_idx + 1]
                folder_key = prefix + folder_name
                prefixes.add(folder_key)
                # Accumulate stats for this folder
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
        return render(request, 'p2_core/blob_list.html', context)


class BlobDetailView(LoginRequiredMixin, View):
    def get(self, request, volume_pk, blob_path):
        volume = get_object_or_404(Volume, pk=volume_pk)
        if not _check_permission(request.user, volume, 'read'):
            raise PermissionDenied

        engine = _get_engine(volume)
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
        if _check_permission(request.user, volume, 'admin'):
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
        return render(request, 'p2_core/blob_detail.html', context)



class BlobInlineView(LoginRequiredMixin, View):
    """Serve blob inline for preview (no Content-Disposition: attachment)."""
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
            response = FileResponse(open(fs_path, 'rb'), content_type=mime)
            response['X-Frame-Options'] = 'SAMEORIGIN'
            return response
        except Exception:
            raise Http404


class BlobDownloadView(LoginRequiredMixin, View):
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
        try:
            return FileResponse(open(fs_path, 'rb'), as_attachment=True, filename=blob_path.split('/')[-1])
        except Exception:
            raise Http404


class BlobDeleteView(LoginRequiredMixin, View):
    def post(self, request, volume_pk, blob_path):
        return self._delete(request, volume_pk, blob_path)

    def get(self, request, volume_pk, blob_path):
        return self._delete(request, volume_pk, blob_path)

    def _delete(self, request, volume_pk, blob_path):
        volume = get_object_or_404(Volume, pk=volume_pk)
        if not _check_permission(request.user, volume, 'delete'):
            raise PermissionDenied

        engine = _get_engine(volume)
        metadata_json = engine.get(blob_path)
        if metadata_json:
            attributes = json.loads(metadata_json)
            internal_path = attributes.get('internal_path')
            if internal_path:
                fs_path = internal_path.replace('/internal-storage/', '/storage/')
                try:
                    os.remove(fs_path)
                except OSError:
                    pass
            engine.delete(blob_path)
        # Redirect regardless — double-delete or already-gone is not an error
        prefix = '/'.join(blob_path.split('/')[:-1])
        qs = f'?prefix={prefix}/' if prefix else ''
        return redirect(
            reverse('p2_ui:core-blob-list', kwargs={'volume_pk': volume_pk}) + qs
        )


class FolderDownloadView(LoginRequiredMixin, View):
    """Stream a folder as a ZIP archive without loading it all into memory.

    Uses a pipe (os.pipe) + a background thread that writes into the write-end
    while Django streams from the read-end.  Each file is fed in 64 KB chunks
    so peak memory is O(chunk_size), not O(folder_size).
    """
    def get(self, request, volume_pk, folder_prefix):
        volume = get_object_or_404(Volume, pk=volume_pk)
        if not _check_permission(request.user, volume, 'read'):
            raise PermissionDenied

        # Normalise prefix so it always ends with /
        prefix = folder_prefix.strip('/') + '/'

        engine = _get_engine(volume)
        try:
            items = list(engine.list(prefix))
        except Exception as e:
            LOGGER.error("Failed to list for ZIP: %s", e)
            items = []

        # Collect only real files under this prefix
        blobs = []
        for key, json_val in items:
            if not key.startswith(prefix):
                continue
            try:
                attr = json.loads(json_val)
                if not attr.get(ATTR_BLOB_IS_FOLDER, False):
                    internal_path = attr.get('internal_path', '')
                    fs_path = internal_path.replace('/internal-storage/', '/storage/')
                    mime = attr.get(ATTR_BLOB_MIME, 'application/octet-stream')
                    blobs.append((key, fs_path, mime))
            except Exception:
                pass

        def _zip_generator():
            import zipfile
            import queue
            import threading
            import mimetypes

            # Already-compressed types — storing avoids wasting CPU on re-compression
            _STORE_TYPES = {
                'video/', 'audio/', 'image/jpeg', 'image/png', 'image/gif',
                'image/webp', 'application/zip', 'application/gzip',
                'application/x-7z-compressed', 'application/x-rar-compressed',
            }

            def _should_store(mime: str) -> bool:
                for t in _STORE_TYPES:
                    if mime.startswith(t):
                        return True
                return False

            # 16 * 1 MB = 16 MB max in-flight — good balance of throughput vs memory
            CHUNK = 1 << 20  # 1 MB read chunks
            q = queue.Queue(maxsize=16)
            SENTINEL = object()

            class _StreamIO:
                """File-like that forwards written bytes into the queue."""
                def __init__(self):
                    self._pos = 0

                def write(self, data):
                    if data:
                        q.put(bytes(data))
                    self._pos += len(data)
                    return len(data)

                def flush(self):
                    pass

                def tell(self):
                    return self._pos

            def _worker():
                try:
                    stream = _StreamIO()
                    with zipfile.ZipFile(stream, mode='w', allowZip64=True) as zf:
                        for key, fs_path, mime in blobs:
                            arcname = key[len(prefix):]
                            compress = (zipfile.ZIP_STORED if _should_store(mime)
                                        else zipfile.ZIP_DEFLATED)
                            zi = zipfile.ZipInfo(arcname)
                            zi.compress_type = compress
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
                    LOGGER.error("ZIP generation error: %s", e)
                finally:
                    q.put(SENTINEL)

            t = threading.Thread(target=_worker, daemon=True)
            t.start()

            while True:
                item = q.get()
                if item is SENTINEL:
                    break
                yield item

        folder_name = prefix.strip('/').split('/')[-1] or volume.name
        response = StreamingHttpResponse(_zip_generator(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{folder_name}.zip"'
        return response


class FolderDeleteView(LoginRequiredMixin, View):
    """Delete all blobs under a prefix and redirect back."""
    def get(self, request, volume_pk, folder_prefix):
        volume = get_object_or_404(Volume, pk=volume_pk)
        if not _check_permission(request.user, volume, 'delete'):
            raise PermissionDenied

        prefix = folder_prefix.strip('/') + '/'
        engine = _get_engine(volume)
        try:
            items = list(engine.list(prefix))
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
                    fs_path = internal_path.replace('/internal-storage/', '/storage/')
                    try:
                        os.remove(fs_path)
                    except OSError:
                        pass
                engine.delete(key)
            except Exception as e:
                LOGGER.warning("Error deleting %s: %s", key, e)

        # Redirect to parent prefix
        parent = '/'.join(prefix.strip('/').split('/')[:-1])
        parent_qs = ('?prefix=' + parent + '/') if parent else ''
        return redirect(
            reverse('p2_ui:core-blob-list', kwargs={'volume_pk': volume_pk}) + parent_qs
        )

