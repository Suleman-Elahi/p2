"""Tests for conditional headers on PUT/CopyObject."""
from unittest.mock import MagicMock

import pytest

from p2.s3.views.objects import _check_conditional_headers


def _blob(etag='"abc123"', mtime="2025-01-15T10:00:00+00:00"):
    b = MagicMock()
    b.attributes = {"blob.p2.io/hash/md5": etag, "blob.p2.io/stat/mtime": mtime}
    return b


def _req(**meta):
    r = MagicMock()
    r.META = meta
    return r


class TestConditionalHeaders:

    # -- If-Match --

    def test_if_match_hit(self):
        assert _check_conditional_headers(_req(HTTP_IF_MATCH='"abc123"'), _blob()) is None

    def test_if_match_miss(self):
        resp = _check_conditional_headers(_req(HTTP_IF_MATCH='"other"'), _blob())
        assert resp is not None and resp.status_code == 412

    def test_if_match_star(self):
        assert _check_conditional_headers(_req(HTTP_IF_MATCH="*"), _blob()) is None

    def test_if_match_no_blob(self):
        resp = _check_conditional_headers(_req(HTTP_IF_MATCH='"abc"'), None)
        assert resp is not None and resp.status_code == 412

    # -- If-None-Match --

    def test_if_none_match_miss(self):
        assert _check_conditional_headers(_req(HTTP_IF_NONE_MATCH='"other"'), _blob()) is None

    def test_if_none_match_hit(self):
        resp = _check_conditional_headers(_req(HTTP_IF_NONE_MATCH='"abc123"'), _blob())
        assert resp is not None and resp.status_code == 412

    def test_if_none_match_star_exists(self):
        resp = _check_conditional_headers(_req(HTTP_IF_NONE_MATCH="*"), _blob())
        assert resp is not None and resp.status_code == 412

    def test_if_none_match_star_no_blob(self):
        assert _check_conditional_headers(_req(HTTP_IF_NONE_MATCH="*"), None) is None

    # -- No headers --

    def test_no_headers(self):
        assert _check_conditional_headers(_req(), _blob()) is None

    def test_no_headers_no_blob(self):
        assert _check_conditional_headers(_req(), None) is None

    # -- Multiple ETags --

    def test_if_match_multiple_hit(self):
        assert _check_conditional_headers(
            _req(HTTP_IF_MATCH='"x", "abc123", "y"'), _blob()) is None

    def test_if_match_multiple_miss(self):
        resp = _check_conditional_headers(
            _req(HTTP_IF_MATCH='"x", "y"'), _blob())
        assert resp is not None and resp.status_code == 412
