"""Tests for p2.s3.policy — IAM-style bucket policy evaluation engine."""
import json
import pytest

from p2.s3.policy import (
    AccessCheckResult,
    Effect,
    _resource_match,
    check_access,
    parse_policy,
)


# ---------------------------------------------------------------------------
# parse_policy
# ---------------------------------------------------------------------------

class TestParsePolicy:

    def test_valid_allow(self):
        stmts = parse_policy(json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::bucket/*",
            }],
        }))
        assert len(stmts) == 1
        assert stmts[0]["effect"] == Effect.ALLOW
        assert stmts[0]["actions"] == ["s3:GetObject"]
        assert stmts[0]["resources"] == ["arn:aws:s3:::bucket/*"]

    def test_valid_deny(self):
        stmts = parse_policy(json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Deny",
                "Action": ["s3:DeleteObject", "s3:PutObject"],
                "Resource": ["arn:aws:s3:::prod/*"],
            }],
        }))
        assert stmts[0]["effect"] == Effect.DENY
        assert len(stmts[0]["actions"]) == 2

    def test_bad_version(self):
        with pytest.raises(ValueError, match="Unsupported policy version"):
            parse_policy(json.dumps({"Version": "2020-01-01", "Statement": []}))

    def test_not_object(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            parse_policy('"just a string"')

    def test_bad_effect(self):
        with pytest.raises(ValueError, match="Invalid Effect"):
            parse_policy(json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Maybe", "Action": "s3:GetObject", "Resource": "*"}],
            }))

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            parse_policy("{not json}")

    def test_multiple_statements(self):
        stmts = parse_policy(json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"},
                {"Effect": "Deny", "Action": "s3:DeleteObject", "Resource": "*"},
            ],
        }))
        assert len(stmts) == 2

    def test_sid_and_principal(self):
        stmts = parse_policy(json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "AllowAll",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": "*",
            }],
        }))
        assert stmts[0]["sid"] == "AllowAll"
        assert stmts[0]["principal"] == "*"


# ---------------------------------------------------------------------------
# _resource_match
# ---------------------------------------------------------------------------

class TestResourceMatch:

    def test_exact(self):
        assert _resource_match("arn:aws:s3:::bucket", "arn:aws:s3:::bucket")

    def test_star_wildcard(self):
        assert _resource_match("arn:aws:s3:::bucket/*", "arn:aws:s3:::bucket/foo.txt")

    def test_star_no_match(self):
        assert not _resource_match("arn:aws:s3:::bucket/*", "arn:aws:s3:::other/foo.txt")

    def test_question_wildcard(self):
        assert _resource_match("arn:aws:s3:::bucket/file?.txt", "arn:aws:s3:::bucket/fileA.txt")

    def test_question_no_match(self):
        assert not _resource_match("arn:aws:s3:::bucket/file?.txt", "arn:aws:s3:::bucket/fileAB.txt")

    def test_double_star(self):
        assert _resource_match("arn:aws:s3:::*", "arn:aws:s3:::anything/at/all")

    def test_empty(self):
        assert _resource_match("", "")
        assert not _resource_match("", "x")


# ---------------------------------------------------------------------------
# check_access
# ---------------------------------------------------------------------------

class TestCheckAccess:

    def _stmts(self, *raw):
        return parse_policy(json.dumps({
            "Version": "2012-10-17",
            "Statement": list(raw),
        }))

    def test_allow(self):
        stmts = self._stmts({
            "Effect": "Allow", "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::b/*",
        })
        assert check_access(stmts, "s3:GetObject", "arn:aws:s3:::b/f") == AccessCheckResult.ALLOW

    def test_no_match_action(self):
        stmts = self._stmts({
            "Effect": "Allow", "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::b/*",
        })
        assert check_access(stmts, "s3:PutObject", "arn:aws:s3:::b/f") == AccessCheckResult.NO_MATCH

    def test_no_match_resource(self):
        stmts = self._stmts({
            "Effect": "Allow", "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::b/*",
        })
        assert check_access(stmts, "s3:GetObject", "arn:aws:s3:::other/f") == AccessCheckResult.NO_MATCH

    def test_deny_overrides_allow(self):
        stmts = self._stmts(
            {"Effect": "Allow", "Action": "s3:*", "Resource": "arn:aws:s3:::*"},
            {"Effect": "Deny", "Action": "s3:DeleteObject", "Resource": "arn:aws:s3:::prod/*"},
        )
        assert check_access(stmts, "s3:DeleteObject", "arn:aws:s3:::prod/x") == AccessCheckResult.DENY
        assert check_access(stmts, "s3:GetObject", "arn:aws:s3:::prod/x") == AccessCheckResult.ALLOW

    def test_wildcard_action(self):
        stmts = self._stmts({
            "Effect": "Allow", "Action": "s3:*",
            "Resource": "arn:aws:s3:::b/*",
        })
        assert check_access(stmts, "s3:PutObject", "arn:aws:s3:::b/f") == AccessCheckResult.ALLOW

    def test_empty_statements(self):
        assert check_access([], "s3:GetObject", "arn:aws:s3:::b/f") == AccessCheckResult.NO_MATCH
