"""Serve gRPC functionality (async)

NOTE: get_blob_from_rule() and RetrieveFile() depended on the Blob ORM model
which has been removed.  These are stubbed out — gRPC serving will be
reimplemented using the p2_s3_meta LSM engine in a subsequent pass.
"""
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import StringIO
from logging import getLogger
from typing import Any, Dict, Optional
from urllib.parse import unquote

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session

from p2.core.acl import has_volume_permission
from p2.grpc.protos.serve_pb2 import ServeReply, ServeRequest
from p2.grpc.protos.serve_pb2_grpc import ServeServicer
from p2.serve.models import ServeRule

LOGGER = getLogger(__name__)


@contextmanager
def hijack_log():
    """Context manager that captures log output into a StringIO buffer."""
    buffer = StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.DEBUG)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        yield buffer
    finally:
        root_logger.removeHandler(handler)
        handler.close()


@dataclass
class RequestContext:
    """Carries user identity and trace context for a gRPC serve request."""
    user: Any
    trace_id: Optional[str] = None
    path: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)


class Serve(ServeServicer):
    """Async gRPC Service for Serve Application."""

    def _rule_lookup(self, request: ServeRequest, rule: ServeRule, match) -> dict:
        """Build blob lookup kwargs from rule and regex match."""
        lookups = {}
        for lookup_token in rule.blob_query.split('&'):
            lookup_key, lookup_value = lookup_token.split('=')
            lookups[lookup_key] = lookup_value.format(
                path=request.url,
                path_relative=request.url[1:],
                host=request.headers.get('Host', ''),
                meta=request.headers,
                match=match,
            )
        return lookups

    # alias used by the debug view
    rule_lookup = _rule_lookup

    async def get_user(self, request: ServeRequest) -> User:
        """Get user from session cookie asynchronously."""
        session = await Session.objects.filter(session_key=request.session).afirst()
        if session is None:
            from django.contrib.auth.models import AnonymousUser
            return AnonymousUser()
        uid = session.get_decoded().get('_auth_user_id')
        if uid is None:
            from django.contrib.auth.models import AnonymousUser
            return AnonymousUser()
        return await User.objects.aget(pk=uid)

    async def get_blob_from_rule(self, request: ServeRequest, user: Any) -> None:
        """Stub: Blob lookup will be reimplemented against the LSM engine."""
        LOGGER.warning("get_blob_from_rule: stubbed (Blob model removed), returning None")
        return None

    async def RetrieveFile(self, request: ServeRequest, context) -> ServeReply:
        """Handle a file retrieval request — stubbed pending LSM reimplementation."""
        LOGGER.warning("RetrieveFile: stubbed (Blob model removed), returning no-match reply")
        return ServeReply(matching=False, data=b'', headers={})
