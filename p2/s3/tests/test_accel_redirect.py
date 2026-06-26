"""Regression tests for X-Accel-Redirect behaviour.

When P2_STORAGE__USE_X_ACCEL_REDIRECT=true, the server must only emit
X-Accel-Redirect (0-byte body) when Nginx is actually proxying — detected
via the X-Real-IP header.  Direct requests (no X-Real-IP) must receive
the full object body regardless of the setting.

Regression for: commit series fixing segfaults + empty-direct-GET responses.
"""
import urllib.error
import urllib.request
from urllib.parse import urljoin

from django.test import override_settings

from p2.core.models import Volume
from p2.s3.presign import generate_presigned_url
from p2.s3.tests.utils import S3TestCase


class AccelRedirectRegressionTests(S3TestCase):
    """Verify X-Accel-Redirect is only used when Nginx is in front."""

    ACCEL_KEY = 'accel-regression-test.bin'
    ACCEL_DATA = b'regression-test-payload-0123456789abcdef'

    def setUp(self):
        super().setUp()
        # Upload a known object before each test.
        self.boto3.put_object(Body=self.ACCEL_DATA, Bucket='test-1', Key=self.ACCEL_KEY)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _presigned_get_url(self, key: str = None) -> str:
        """Build a presigned GET URL for *key* inside bucket 'test-1'."""
        if key is None:
            key = self.ACCEL_KEY
        base = urljoin(self.live_server_url, f"/test-1/{key}")
        return generate_presigned_url(base, 'test-1', key, 'GET', expires_in=300)

    def _raw_get(self, url: str, extra_headers: dict | None = None) -> tuple[int, dict, bytes]:
        """Perform a raw HTTP GET and return (status, headers, body)."""
        req = urllib.request.Request(url, method='GET')
        if extra_headers:
            for k, v in extra_headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    # ── Tests ────────────────────────────────────────────────────────────

    @override_settings(USE_X_ACCEL_REDIRECT=True)
    def test_direct_request_serves_full_body_when_accel_enabled(self):
        """Without X-Real-IP, the full object body must be returned even
        when USE_X_ACCEL_REDIRECT is True."""
        url = self._presigned_get_url()
        status, headers, body = self._raw_get(url)

        self.assertEqual(status, 200,
                         f"Expected 200, got {status}. Body snippet: {body[:200]}")
        self.assertEqual(body, self.ACCEL_DATA,
                         f"Body mismatch: expected {len(self.ACCEL_DATA)} bytes, "
                         f"got {len(body)} bytes")
        # Must NOT emit the Nginx redirect header when Nginx isn't proxying.
        self.assertNotIn('X-Accel-Redirect', headers,
                         "X-Accel-Redirect header emitted on direct request!")
        # Content-Length must match the actual body.
        self.assertIn('Content-Length', headers,
                      "Content-Length header missing on direct response")
        self.assertEqual(headers['Content-Length'], str(len(self.ACCEL_DATA)),
                         f"Content-Length mismatch: {headers.get('Content-Length')}")

    @override_settings(USE_X_ACCEL_REDIRECT=True)
    def test_nginx_proxied_request_uses_accel_redirect(self):
        """With X-Real-IP present, the server should delegate to Nginx
        via X-Accel-Redirect (zero-copy path)."""
        url = self._presigned_get_url()
        status, headers, body = self._raw_get(url, extra_headers={
            'X-Real-IP': '127.0.0.1',
        })

        self.assertEqual(status, 200,
                         f"Expected 200, got {status}. Body: {body[:200]}")
        # The X-Accel-Redirect header must be present.
        self.assertIn('X-Accel-Redirect', headers,
                      "X-Accel-Redirect header missing when Nginx proxy detected!")
        # The body must be empty — Nginx is supposed to serve the file.
        self.assertEqual(body, b'',
                         f"Accel-redirect response must have empty body, "
                         f"got {len(body)} bytes")

    @override_settings(USE_X_ACCEL_REDIRECT=False)
    def test_direct_request_serves_full_body_when_accel_disabled(self):
        """When USE_X_ACCEL_REDIRECT is False, full body must be served
        regardless of X-Real-IP presence."""
        for label, extra in [('without X-Real-IP', {}),
                              ('with X-Real-IP', {'X-Real-IP': '127.0.0.1'})]:
            with self.subTest(label):
                url = self._presigned_get_url()
                status, headers, body = self._raw_get(url, extra_headers=extra)

                self.assertEqual(status, 200, f"[{label}] Expected 200, got {status}")
                self.assertEqual(body, self.ACCEL_DATA,
                                 f"[{label}] Body mismatch: expected "
                                 f"{len(self.ACCEL_DATA)} bytes, got {len(body)}")
                self.assertNotIn('X-Accel-Redirect', headers,
                                 f"[{label}] X-Accel-Redirect emitted when setting is False!")

    @override_settings(USE_X_ACCEL_REDIRECT=True)
    def test_x_accel_redirect_header_content(self):
        """The X-Accel-Redirect header points to the correct internal path."""
        url = self._presigned_get_url()
        _, headers, _ = self._raw_get(url, extra_headers={'X-Real-IP': '127.0.0.1'})

        redirect_path = headers.get('X-Accel-Redirect', '')
        volume = Volume.objects.get(name='test-1')
        expected_prefix = f'/internal-storage/volumes/{volume.uuid.hex}'
        self.assertTrue(redirect_path.startswith(expected_prefix),
                        f"X-Accel-Redirect path '{redirect_path}' does not start "
                        f"with '{expected_prefix}'")
        parts = redirect_path.split('/')
        self.assertEqual(len(parts), 7, f"Expected 7 components in internal path, got: {parts}")
