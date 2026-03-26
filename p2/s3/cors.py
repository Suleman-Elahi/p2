"""p2 S3 CORS support.

CORS rules are stored as a JSON list in volume.tags[TAG_S3_CORS_RULES].
Each rule follows the S3 CORSRule schema:
  {
    "AllowedOrigins": ["https://example.com"],
    "AllowedMethods": ["GET", "PUT"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
"""
import fnmatch
import json
import logging
from xml.etree import ElementTree

from django.http import HttpResponse

from p2.s3.constants import TAG_S3_CORS_RULES, XML_NAMESPACE
from p2.s3.http import XMLResponse

LOGGER = logging.getLogger(__name__)


def _match_origin(pattern: str, origin: str) -> bool:
    return fnmatch.fnmatch(origin, pattern)


def get_cors_rules(volume) -> list:
    raw = volume.tags.get(TAG_S3_CORS_RULES)
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def find_matching_rule(rules: list, origin: str, method: str) -> dict | None:
    for rule in rules:
        origins = rule.get("AllowedOrigins", [])
        methods = rule.get("AllowedMethods", [])
        if method.upper() not in [m.upper() for m in methods]:
            continue
        for pat in origins:
            if _match_origin(pat, origin):
                return rule
    return None


def apply_cors_headers(response: HttpResponse, rule: dict, origin: str) -> HttpResponse:
    response["Access-Control-Allow-Origin"] = origin
    response["Access-Control-Allow-Methods"] = ", ".join(rule.get("AllowedMethods", []))
    allowed_headers = rule.get("AllowedHeaders", [])
    if allowed_headers:
        response["Access-Control-Allow-Headers"] = ", ".join(allowed_headers)
    expose_headers = rule.get("ExposeHeaders", [])
    if expose_headers:
        response["Access-Control-Expose-Headers"] = ", ".join(expose_headers)
    max_age = rule.get("MaxAgeSeconds")
    if max_age:
        response["Access-Control-Max-Age"] = str(max_age)
    response["Vary"] = "Origin"
    return response


def cors_preflight_response(rule: dict, origin: str) -> HttpResponse:
    response = HttpResponse(status=200)
    return apply_cors_headers(response, rule, origin)


def build_cors_xml(rules: list) -> ElementTree.Element:
    root = ElementTree.Element("{%s}CORSConfiguration" % XML_NAMESPACE)
    for rule in rules:
        r = ElementTree.SubElement(root, "CORSRule")
        for origin in rule.get("AllowedOrigins", []):
            ElementTree.SubElement(r, "AllowedOrigin").text = origin
        for method in rule.get("AllowedMethods", []):
            ElementTree.SubElement(r, "AllowedMethod").text = method
        for header in rule.get("AllowedHeaders", []):
            ElementTree.SubElement(r, "AllowedHeader").text = header
        for header in rule.get("ExposeHeaders", []):
            ElementTree.SubElement(r, "ExposeHeader").text = header
        if "MaxAgeSeconds" in rule:
            ElementTree.SubElement(r, "MaxAgeSeconds").text = str(rule["MaxAgeSeconds"])
    return root


def parse_cors_xml(body: bytes) -> list:
    """Parse a PutBucketCors XML body into a list of rule dicts."""
    root = ElementTree.fromstring(body)
    ns = {"s3": XML_NAMESPACE}
    rules = []
    for rule_el in root.findall("CORSRule") or root.findall(f"{{{XML_NAMESPACE}}}CORSRule"):
        rule = {}
        def _texts(tag):
            return [el.text for el in rule_el.findall(tag) or rule_el.findall(f"{{{XML_NAMESPACE}}}{tag}") if el.text]
        rule["AllowedOrigins"] = _texts("AllowedOrigin")
        rule["AllowedMethods"] = _texts("AllowedMethod")
        rule["AllowedHeaders"] = _texts("AllowedHeader")
        rule["ExposeHeaders"] = _texts("ExposeHeader")
        max_age_els = rule_el.findall("MaxAgeSeconds") or rule_el.findall(f"{{{XML_NAMESPACE}}}MaxAgeSeconds")
        if max_age_els and max_age_els[0].text:
            rule["MaxAgeSeconds"] = int(max_age_els[0].text)
        rules.append(rule)
    return rules
