"""The MCP endpoint's JSON-RPC envelope, version negotiation and sessions (KTD1-KTD3).

Pure helpers for ``chat_mcp.views``: no database, no request objects.

* Messages are single JSON-RPC 2.0 objects (no batches). ``parse_message``
  classifies one as a request (has an ``id``), a notification (no ``id``), or
  a response the client sent back (``result`` or ``error``, no ``method``).
* ``negotiate_version`` echoes a supported protocol version and otherwise
  answers the latest one this server speaks.
* The session ID is a Django ``signing`` timestamped signature of a random
  token: storage-free, so any web replica can verify it. It only gives rate
  limiting a per-conversation key; a missing, tampered or expired ID verifies
  to ``None`` and the request is still served (KTD3).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core import signing

#: Protocol versions this server speaks, oldest first (KTD2).
SUPPORTED_VERSIONS = ('2025-03-26', '2025-06-18', '2025-11-25')
LATEST_VERSION = SUPPORTED_VERSIONS[-1]

#: JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_SESSION_SALT = 'chat_mcp.session'


class RpcError(Exception):
    """A JSON-RPC protocol fault, answered as a JSON-RPC error object.

    Attributes:
        code: The JSON-RPC error code.
        message: A short description for the client.
    """

    def __init__(self, code: int, message: str) -> None:
        """Build the error.

        Args:
            code: The JSON-RPC error code.
            message: A short description for the client.
        """
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Message:
    """One decoded JSON-RPC message.

    Attributes:
        method: The method name; ``None`` for a response the client sent.
        params: The ``params`` object (empty when absent).
        id: The request ID; ``None`` for a notification or response.
        is_request: Whether the message expects a response.
    """

    method: str | None
    params: dict[str, Any]
    id: str | int | None
    is_request: bool


def _valid_id(value: Any) -> bool:
    """Whether ``value`` is an MCP request ID: a string or an integer."""
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def parse_message(body: Any) -> Message:
    """Validate one decoded JSON-RPC message.

    Args:
        body: The decoded request body (a batch array is rejected by the view
            before this is called).

    Returns:
        The message.

    Raises:
        RpcError: ``INVALID_REQUEST`` if it is not a JSON-RPC 2.0 object with a
            string ``method`` (or a response), or its ``id`` is not a string or
            integer; ``INVALID_PARAMS`` if ``params`` is present and not an
            object.
    """
    if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
        raise RpcError(INVALID_REQUEST, 'expected a JSON-RPC 2.0 message object')
    has_id = 'id' in body
    if has_id and not _valid_id(body['id']):
        raise RpcError(INVALID_REQUEST, 'id must be a string or an integer')
    method = body.get('method')
    if method is None and has_id and ('result' in body or 'error' in body):
        return Message(method=None, params={}, id=None, is_request=False)
    if not isinstance(method, str):
        raise RpcError(INVALID_REQUEST, 'method must be a string')
    params = body.get('params', {})
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, 'params must be an object')
    return Message(
        method=method, params=params, id=body['id'] if has_id else None, is_request=has_id,
    )


def result_body(msg_id: str | int | None, result: dict[str, Any]) -> dict[str, Any]:
    """A JSON-RPC success response."""
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def error_body(msg_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    """A JSON-RPC error response (``id`` null when the request's is unknown)."""
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}


def negotiate_version(requested: Any) -> str:
    """The protocol version to answer ``initialize`` with (KTD2).

    Args:
        requested: The client's ``protocolVersion``.

    Returns:
        ``requested`` when this server supports it, else ``LATEST_VERSION``.
    """
    return requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=_SESSION_SALT)


def issue_session_id() -> str:
    """A new signed session ID: a random token plus its issue time (KTD3).

    Returns:
        The value for the ``Mcp-Session-Id`` response header (visible ASCII).
    """
    return _signer().sign(secrets.token_urlsafe(16))


def verify_session_id(value: str | None) -> str | None:
    """The token inside a session ID, when the ID is valid and current.

    Args:
        value: The ``Mcp-Session-Id`` request header, or ``None``.

    Returns:
        The session's random token, or ``None`` when the header is absent,
        tampered with, or older than ``MCP_SESSION_MAX_AGE_SECONDS``.
    """
    if not value:
        return None
    try:
        return _signer().unsign(value, max_age=int(settings.MCP_SESSION_MAX_AGE_SECONDS))
    except signing.BadSignature:
        return None


def session_hash(token: str | None) -> str | None:
    """A short, non-reversible tag of a session token, for logs (KTD14).

    Args:
        token: A verified session token, or ``None``.

    Returns:
        The first 12 hex digits of its SHA-256, or ``None``.
    """
    if token is None:
        return None
    return hashlib.sha256(token.encode()).hexdigest()[:12]
