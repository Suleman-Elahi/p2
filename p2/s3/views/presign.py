"""p2 S3 Presigned URL view — generates presigned GET/PUT URLs via REST API."""
import logging

from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin

from p2.s3.presign import generate_presigned_url

LOGGER = logging.getLogger(__name__)


class PresignedURLView(LoginRequiredMixin, View):
    """Generate a presigned URL for GET or PUT on a blob.

    POST /_/api/v1/s3/presign/
    {
        "bucket": "my-volume",
        "key": "/path/to/file.txt",
        "method": "GET",          # or "PUT"
        "expires_in": 3600,       # seconds, default 3600, max 604800
        "base_url": "http://localhost:8000"
    }
    """

    def post(self, request):
        import json
        try:
            body = json.loads(request.body)
        except (ValueError, TypeError):
            return JsonResponse({"error": "invalid JSON"}, status=400)

        bucket = body.get("bucket", "")
        key = body.get("key", "")
        method = body.get("method", "GET").upper()
        expires_in = int(body.get("expires_in", 3600))
        base_url = body.get("base_url", "").rstrip("/")

        if not bucket or not key:
            return JsonResponse({"error": "bucket and key are required"}, status=400)
        if method not in ("GET", "PUT", "HEAD"):
            return JsonResponse({"error": "method must be GET, PUT, or HEAD"}, status=400)

        key = key.lstrip('/')  # no leading slash — matches URL router capture
        object_url = f"{base_url}/{bucket}/{key}"
        url = generate_presigned_url(object_url, bucket, key, method, expires_in)
        return JsonResponse({"url": url, "expires_in": expires_in})
