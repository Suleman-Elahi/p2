"""p2 S3 Presigned URL view — generates presigned GET/PUT URLs via REST API."""
import json
import logging

from django.http import JsonResponse
from django.views import View
from ninja_jwt.authentication import JWTAuth
from ninja_jwt.exceptions import InvalidToken

from p2.s3.presign import generate_presigned_url

LOGGER = logging.getLogger(__name__)


class PresignedURLView(View):
    """Generate a presigned URL for GET or PUT on a blob.

    POST /_/api/v1/s3/presign/
    {
        "bucket": "my-volume",
        "key": "/path/to/file.txt",
        "method": "GET",          # or "PUT"
        "expires_in": 3600,       # seconds, default 3600, max 604800
        "base_url": "http://localhost:8000"
    }

    Accepts either Django session auth or JWT Bearer token.
    """

    def _authenticate(self, request):
        """Try JWT first, then fall back to session auth."""
        # Try JWT Bearer token
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            try:
                jwt_auth = JWTAuth()
                user = jwt_auth.authenticate(request, auth_header[7:])
                if user:
                    request.user = user
                    return True
            except InvalidToken:
                pass
        # Fall back to Django session
        if request.user and request.user.is_authenticated:
            return True
        return False

    def post(self, request):
        if not self._authenticate(request):
            return JsonResponse({"error": "authentication required"}, status=401)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, TypeError, ValueError):
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
