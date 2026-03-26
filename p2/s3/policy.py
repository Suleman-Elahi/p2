"""AWS IAM-style bucket policy evaluation engine."""
import json
import re
from enum import Enum


class Effect(Enum):
    ALLOW = "Allow"
    DENY = "Deny"


class AccessCheckResult(Enum):
    ALLOW = "allow"
    DENY = "deny"
    NO_MATCH = "no_match"


S3_ACTIONS = {
    "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
    "s3:GetBucketLocation", "s3:ListBucketMultipartUploads",
    "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
    "s3:GetBucketAcl", "s3:PutBucketAcl", "s3:GetBucketCors",
    "s3:PutBucketCors", "s3:DeleteBucketCors", "s3:GetBucketTagging",
    "s3:PutBucketTagging", "s3:DeleteBucketTagging",
    "s3:GetObjectAcl", "s3:PutObjectAcl", "s3:GetObjectTagging",
    "s3:PutObjectTagging", "s3:DeleteObjectTagging",
    "s3:PutBucketPolicy", "s3:GetBucketPolicy", "s3:DeleteBucketPolicy",
    "s3:*",
}

# Map p2 permission names to S3 actions
PERMISSION_TO_ACTIONS = {
    "read": {"s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation",
             "s3:GetBucketAcl", "s3:GetBucketCors", "s3:GetBucketTagging",
             "s3:GetObjectAcl", "s3:GetObjectTagging",
             "s3:ListBucketMultipartUploads", "s3:ListMultipartUploadParts"},
    "write": {"s3:PutObject", "s3:PutBucketAcl", "s3:PutBucketCors",
              "s3:PutBucketTagging", "s3:PutObjectAcl", "s3:PutObjectTagging",
              "s3:AbortMultipartUpload"},
    "delete": {"s3:DeleteObject", "s3:DeleteBucketCors", "s3:DeleteBucketTagging",
               "s3:DeleteObjectTagging"},
    "admin": {"s3:PutBucketPolicy", "s3:GetBucketPolicy", "s3:DeleteBucketPolicy"},
}


def _resource_match(pattern: str, resource: str) -> bool:
    """Match an ARN resource pattern with * and ? wildcards (like hs5's resourceMatch)."""
    regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return bool(re.fullmatch(regex, resource))


def parse_policy(policy_json: str) -> list:
    """Parse an AWS IAM policy JSON string into a list of statement dicts.
    Raises ValueError on invalid input."""
    doc = json.loads(policy_json)
    if not isinstance(doc, dict):
        raise ValueError("Policy must be a JSON object")
    version = doc.get("Version")
    if version != "2012-10-17":
        raise ValueError(f"Unsupported policy version: {version}")
    statements = doc.get("Statement", [])
    if not isinstance(statements, list):
        raise ValueError("Statement must be an array")
    parsed = []
    for stmt in statements:
        effect = stmt.get("Effect")
        if effect not in ("Allow", "Deny"):
            raise ValueError(f"Invalid Effect: {effect}")
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        resources = stmt.get("Resource", [])
        if isinstance(resources, str):
            resources = [resources]
        principal = stmt.get("Principal", "*")
        parsed.append({
            "sid": stmt.get("Sid", ""),
            "effect": Effect.ALLOW if effect == "Allow" else Effect.DENY,
            "actions": actions,
            "resources": resources,
            "principal": principal,
        })
    return parsed


def check_access(statements: list, action: str, resource: str) -> AccessCheckResult:
    """Evaluate policy statements against an action and resource ARN.
    Deny overrides Allow (AWS standard evaluation)."""
    result = AccessCheckResult.NO_MATCH
    for stmt in statements:
        action_match = any(
            a == "s3:*" or a == action
            for a in stmt["actions"]
        )
        if not action_match:
            continue
        resource_match = any(
            _resource_match(r, resource)
            for r in stmt["resources"]
        )
        if not resource_match:
            continue
        if stmt["effect"] == Effect.DENY:
            return AccessCheckResult.DENY
        result = AccessCheckResult.ALLOW
    return result
