"""Regression tests for direct object serving.

Granian serves object bytes itself: the S3 data plane reads block ranges out of
the append-only volume files and writes them to the response. There is no
reverse proxy and no sendfile() handoff.

This file replaces test_accel_redirect.py. p2 used to emit an X-Accel-Redirect
header with a 0-byte body when it detected an nginx proxy (via X-Real-IP), and
that had a nasty failure mode: hit the server directly and you got an empty
200 instead of your object. The redirect path is gone, so the tests here pin the
two properties that mattered:

* a GET always returns the full body with a matching Content-Length
* no redirect/handoff header is ever emitted, *including* when the request looks
  proxied — which is what would silently break if the redirect were reintroduced
  without a proxy actually being present

Original regression: commit series fixing segfaults + empty-direct-GET responses.
"""
import urllib.error
import urllib.request
from urllib.parse import urljoin

from p2.s3.presign import generate_presigned_url
from p2.s3.tests.utils import S3TestCase

# Headers the removed nginx handoff used to set. None of these may appear.
HANDOFF_HEADERS = ('X-Accel-Redirect', 'X-P2-Accel', 'X-Sendfile')

# Request headers that used to switch p2 into redirect mode.
PROXY_LOOKALIKE_HEADERS = {'X-Real-IP': '127.0.0.1',
                           'X-Forwarded-For': '127.0.0.1'}


class DirectObjectServingTests(S3TestCase):
    """Object bytes must always come from Granian, never a proxy handoff."""

    OBJ_KEY = 'direct-serving-regression.bin'
    OBJ_DATA = b'regression-test-payload-0123456789abcdef'

    def setUp(self):
        super().setUp()
        self.boto3.put_object(Body=self.OBJ_DATA, Bucket='test-1', Key=self.OBJ_KEY)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _presigned_get_url(self, key: str = None) -> str:
        """Build a presigned GET URL for *key* inside bucket 'test-1'."""
        if key is None:
            key = self.OBJ_KEY
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
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def _assert_served_directly(self, label, status, headers, body):
        self.assertEqual(status, 200,
                         f"[{label}] Expected 200, got {status}. Body snippet: {body[:200]}")
        self.assertEqual(body, self.OBJ_DATA,
                         f"[{label}] Body mismatch: expected {len(self.OBJ_DATA)} bytes, "
                         f"got {len(body)} bytes")
        self.assertIn('Content-Length', headers,
                      f"[{label}] Content-Length header missing on direct response")
        self.assertEqual(headers['Content-Length'], str(len(self.OBJ_DATA)),
                         f"[{label}] Content-Length mismatch: {headers.get('Content-Length')}")
        for header in HANDOFF_HEADERS:
            self.assertNotIn(header, headers,
                             f"[{label}] {header} emitted — p2 serves objects directly, "
                             f"a proxy handoff header means the removed nginx path is back "
                             f"and the client would receive an empty body")

    # ── Tests ────────────────────────────────────────────────────────────

    def test_direct_request_serves_full_body(self):
        """A plain GET returns the whole object with a matching Content-Length."""
        status, headers, body = self._raw_get(self._presigned_get_url())
        self._assert_served_directly('direct', status, headers, body)

    def test_proxy_lookalike_headers_do_not_change_response(self):
        """Requests that look proxied are served identically.

        X-Real-IP used to flip the response to a 0-byte redirect. Since nothing
        is in front of Granian, an attacker-supplied or stale header must not be
        able to turn a GET into an empty response.
        """
        for header, value in PROXY_LOOKALIKE_HEADERS.items():
            with self.subTest(header):
                status, headers, body = self._raw_get(
                    self._presigned_get_url(), extra_headers={header: value})
                self._assert_served_directly(header, status, headers, body)

    def test_all_proxy_lookalike_headers_combined(self):
        """The same holds with every proxy-ish header set at once."""
        status, headers, body = self._raw_get(
            self._presigned_get_url(), extra_headers=dict(PROXY_LOOKALIKE_HEADERS))
        self._assert_served_directly('combined', status, headers, body)
