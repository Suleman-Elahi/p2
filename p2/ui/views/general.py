"""p2 UI views — Frappe SPA index"""
import os

from django.http import HttpResponse, FileResponse
from django.views.generic import View


# ── Frappe UI SPA catch-all ─────────────────────────────────────────────

_SPA_DIST = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'ui', 'dist')

class SpaIndexView(View):
    """Serve the Frappe UI SPA.

    - /assets/* paths → serve the actual static file from ui/dist/
    - all other paths → serve ui/dist/index.html (Vue Router handles routing)
    """

    def get(self, request, *args, **kwargs):
        asset_path = kwargs.get('asset_path', '')
        if asset_path:
            # Serve a specific asset file
            file_path = os.path.join(_SPA_DIST, 'assets', asset_path)
            if os.path.isfile(file_path) and not _is_unsafe_path(file_path, _SPA_DIST):
                content_type = _guess_mime(file_path)
                return FileResponse(open(file_path, 'rb'), content_type=content_type)
            return HttpResponse(status=404)

        # Serve index.html for SPA routing
        index_path = os.path.join(_SPA_DIST, 'index.html')
        if not os.path.isfile(index_path):
            return HttpResponse(
                '<html><body style="font-family:sans-serif;padding:2rem">'
                '<h1>p2 UI not built</h1>'
                '<p>Run <code>cd ui && npm run build</code> to build the Frappe UI.</p>'
                '<p><a href="/_/admin/">Django Admin</a> | '
                '<a href="/_/api/v1/docs">API Docs</a></p>'
                '</body></html>',
                content_type='text/html',
            )
        return FileResponse(open(index_path, 'rb'), content_type='text/html')


def _is_unsafe_path(file_path, base_dir):
    """Prevent directory traversal attacks."""
    real_path = os.path.realpath(file_path)
    real_base = os.path.realpath(base_dir)
    return not real_path.startswith(real_base)


def _guess_mime(path):
    """Guess MIME type from file extension."""
    ext = os.path.splitext(path)[1].lower()
    return {
        '.js': 'application/javascript',
        '.css': 'text/css',
        '.html': 'text/html',
        '.json': 'application/json',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.woff2': 'font/woff2',
        '.woff': 'font/woff',
    }.get(ext, 'application/octet-stream')
