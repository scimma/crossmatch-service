"""The public, read-only MCP endpoint: ``POST /mcp`` (KTD1-KTD3, KTD13-KTD15).

A stateless Streamable HTTP endpoint without SSE: each POST carries one
JSON-RPC message and gets one ``application/json`` response. It answers
``initialize``, ``ping``, ``tools/list`` and ``tools/call``; any
notification (or response) from the client gets 202 with no body. ``GET`` and
``DELETE`` are 405 and a batch array is 400. A request whose ``Origin``
header is outside ``MCP_ALLOWED_ORIGINS`` is 403; one with no ``Origin``
passes. There is no login and no OAuth metadata (R11).

JSON-RPC errors are reserved for protocol faults: parse error, invalid
request, unknown method, unknown tool. Invalid tool arguments, and an
``ApiError`` from the guard or the API, are tool results with ``isError``
true, so the model can read and relay them. Each ``tools/call`` logs one
``mcp tool call`` line (KTD14). Only ``tools/call`` is rate-limited and
counted against the concurrency cap (``chat_mcp.limits``, KTD4); an over-limit
call is an ``isError`` tool result with a retry-after, never an HTTP 429.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse

from api.errors import ApiError, InvalidQuery
from api.guard import RequestGuard
from api.views import _json_body
from chat_mcp import limits, protocol
from chat_mcp.projection import to_text
from chat_mcp.protocol import RpcError
from chat_mcp.tools import TOOLS, ToolArgumentError
from core import provenance
from core.log import get_logger

logger = get_logger(__name__)

SERVER_NAME = 'scimma-rubin-crossmatch'
SERVER_TITLE = 'SCiMMA Rubin Crossmatch'

INSTRUCTIONS = (
    'Answers questions about Rubin Observatory transients from the SCiMMA '
    'crossmatch service: which Gaia DR3, DES Y6 Gold, DELVE DR3 Gold and '
    'SkyMapper DR4 sources coincide with a transient, looked up by TNS name or '
    'diaObjectId (lookup_rubin_transients) or by sky position '
    '(search_rubin_transients_near_position). Use describe_crossmatch_service '
    'to explain coverage or why an object is absent. Always pass diaObjectIds '
    'as strings. Offer the TNS, Lasair and ANTARES links in each answer for '
    'light curves and classifications, which this service does not hold.'
)

#: ``outcome`` of a call whose arguments failed validation.
OUTCOME_INVALID_ARGUMENTS = 'invalid_arguments'


def _rpc_response(body: dict[str, Any], status: int = 200) -> JsonResponse:
    return JsonResponse(body, status=status)


def session_token(request: HttpRequest) -> str | None:
    """The verified session token of a request (KTD3).

    Args:
        request: The request.

    Returns:
        The token inside a valid, current ``Mcp-Session-Id`` header, or
        ``None`` when the header is absent, tampered with, or expired; such a
        request is still served.
    """
    return protocol.verify_session_id(request.headers.get('Mcp-Session-Id'))


def text_result(projection: dict[str, Any]) -> dict[str, Any]:
    """A successful tool result: one text block with the projection's JSON (KTD5)."""
    return {'content': [{'type': 'text', 'text': to_text(projection)}], 'isError': False}


def error_result(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    retry_after: int | None = None,
) -> dict[str, Any]:
    """A tool result with ``isError`` the model can read and relay.

    Args:
        code: ``invalid_arguments`` or an API error code (``query_too_expensive``,
            ``service_unavailable``, ...).
        message: What went wrong, naming any limit.
        retryable: Whether the same call can succeed later.
        retry_after: Seconds to wait before retrying, when known.

    Returns:
        ``content`` with one text block holding ``{"error": {...}}``, and
        ``isError`` true.
    """
    error: dict[str, Any] = {'code': code, 'message': message, 'retryable': retryable}
    if retry_after is not None:
        error['retry_after_seconds'] = retry_after
    return {'content': [{'type': 'text', 'text': to_text({'error': error})}], 'isError': True}


def _initialize(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The ``initialize`` result and a new session ID."""
    result = {
        'protocolVersion': protocol.negotiate_version(params.get('protocolVersion')),
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': {
            'name': SERVER_NAME,
            'title': SERVER_TITLE,
            'version': provenance.service_version() or 'unknown',
        },
        'instructions': INSTRUCTIONS,
    }
    return result, protocol.issue_session_id()


def _call_tool(request: HttpRequest, params: dict[str, Any]) -> dict[str, Any]:
    """Run one ``tools/call`` and log it (KTD14, KTD15).

    Args:
        request: The request.
        params: The message's ``params``: ``name`` and optional ``arguments``.

    Returns:
        The tool result.

    Raises:
        RpcError: ``INVALID_PARAMS`` for an unknown tool or ``arguments`` that
            is not an object.
    """
    name = params.get('name')
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        raise RpcError(protocol.INVALID_PARAMS, f'unknown tool: {name!r}')
    arguments = params.get('arguments')
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RpcError(protocol.INVALID_PARAMS, 'arguments must be an object')

    token = session_token(request)
    caller = limits.identify_caller(limits.client_ip(request), token)
    guard = RequestGuard()
    counts: dict[str, Any] = {}
    truncated = False
    try:
        try:
            parsed = tool.parse(arguments)
        except ToolArgumentError as exc:
            outcome = OUTCOME_INVALID_ARGUMENTS
            result = error_result(OUTCOME_INVALID_ARGUMENTS, exc.message)
        else:
            try:
                with limits.admit(caller):
                    try:
                        projection, counts = tool.run(guard, parsed)
                    except ApiError as exc:
                        outcome = exc.code
                        result = error_result(
                            exc.code, exc.message,
                            retryable=exc.retryable, retry_after=exc.retry_after,
                        )
                    else:
                        outcome = 'ok'
                        truncated = bool(projection.get('truncated', False))
                        result = text_result(projection)
            except limits.Limited as exc:
                outcome = exc.code
                result = error_result(
                    exc.code, exc.message, retryable=True, retry_after=exc.retry_after,
                )
    except BaseException as exc:
        _log(
            request, tool.name, token, caller, guard, 'exception', False, counts,
            repr(exc),
        )
        raise
    _log(request, tool.name, token, caller, guard, outcome, truncated, counts, guard.error)
    return result


def _log(
    request: HttpRequest,
    tool: str,
    token: str | None,
    caller: limits.Caller,
    guard: RequestGuard,
    outcome: str,
    truncated: bool,
    counts: dict[str, Any],
    error: str | None,
) -> None:
    """Log one ``mcp tool call`` line (KTD14): never the session ID itself."""
    fields: dict[str, Any] = {
        'tool': tool,
        **counts,
        'outcome': outcome,
        'truncated': truncated,
        'protocol_version': request.headers.get('Mcp-Protocol-Version', '')[:32] or None,
        'client_class': caller.client_class,
        'session': protocol.session_hash(token),
        **guard.timings(),
    }
    if error is None:
        logger.info('mcp tool call', **fields)
    else:
        logger.warning('mcp tool call', error=error, **fields)


def mcp_view(request: HttpRequest) -> HttpResponse:
    """Answer one MCP JSON-RPC message (KTD1).

    Returns:
        403 for a disallowed ``Origin``; 405 for a non-POST method; 400 with a
        JSON-RPC error (null ``id``) for a parse error, a batch, or an invalid
        message; 202 with no body for a notification or response; otherwise
        200 with a JSON-RPC result or error. ``initialize`` also sets
        ``Mcp-Session-Id``.
    """
    origin = request.headers.get('Origin')
    if origin is not None and origin not in settings.MCP_ALLOWED_ORIGINS:
        logger.info('mcp origin rejected', origin=origin[:200])
        return _rpc_response(
            protocol.error_body(None, protocol.INVALID_REQUEST, 'Origin not allowed'),
            status=403,
        )
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    try:
        body = _json_body(request)
    except InvalidQuery as exc:
        code = (
            protocol.INVALID_REQUEST if exc.param == 'Content-Type'
            else protocol.PARSE_ERROR
        )
        return _rpc_response(protocol.error_body(None, code, exc.message), status=400)
    if isinstance(body, list):
        return _rpc_response(
            protocol.error_body(
                None, protocol.INVALID_REQUEST, 'JSON-RPC batches are not supported',
            ),
            status=400,
        )
    try:
        message = protocol.parse_message(body)
    except RpcError as exc:
        return _rpc_response(protocol.error_body(None, exc.code, exc.message), status=400)
    if not message.is_request:
        return HttpResponse(status=202)

    session_id = None
    try:
        if message.method == 'initialize':
            result, session_id = _initialize(message.params)
        elif message.method == 'ping':
            result = {}
        elif message.method == 'tools/list':
            result = {'tools': [tool.definition() for tool in TOOLS.values()]}
        elif message.method == 'tools/call':
            result = _call_tool(request, message.params)
        else:
            raise RpcError(
                protocol.METHOD_NOT_FOUND, f'method not found: {message.method[:100]}',
            )
    except RpcError as exc:
        return _rpc_response(protocol.error_body(message.id, exc.code, exc.message))
    response = _rpc_response(protocol.result_body(message.id, result))
    if session_id is not None:
        response['Mcp-Session-Id'] = session_id
    return response
