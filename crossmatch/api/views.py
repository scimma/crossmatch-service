"""HTTP views for the read-model API.

Thin adapters over ``api/service.py``: parse and validate request parameters,
call the service, return ``JsonResponse``. All query logic lives in the service
layer so it stays reusable by future endpoints and a Python client.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware

from core.log import get_logger
from api.contract import ErrorCode, error_response
from api.errors import ApiError
from api.guard import api_guard
from api.lookup import get_object, lookup_objects, parse_lookup_body
from api.openapi import build_document
from api.service import InvalidQuery, recent_crossmatches

logger = get_logger(__name__)


def _parse_timestamp(raw: str, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a naive value as UTC.

    Args:
        raw: The raw query-string value.
        field: The parameter name, for the error message.

    Returns:
        An aware ``datetime`` in UTC.

    Raises:
        InvalidQuery: If the value is not a parseable ISO-8601 timestamp.
    """
    parsed = parse_datetime(raw)
    if parsed is None:
        raise InvalidQuery(f"{field} is not a valid ISO-8601 timestamp: {raw!r}")
    if is_naive(parsed):
        parsed = make_aware(parsed, timezone.utc)
    return parsed


def _parse_page_size(raw: str) -> int:
    """Parse the ``page_size`` query param as an int.

    Raises:
        InvalidQuery: If the value is not an integer (non-positive values are
            validated by the service, which raises the same exception type).
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise InvalidQuery(f"page_size is not an integer: {raw!r}")


def recent_crossmatches_view(request: HttpRequest) -> JsonResponse:
    """GET one keyset page of crossmatches for objects with recent alerts.

    Query params (all optional): ``start``/``end`` (ISO-8601 UTC; default the
    last 12h), ``time_field`` (``ingest_time`` default | ``event_time``),
    ``detail`` (``ids`` | ``position`` | ``matches`` default | ``full``),
    ``page_size`` (positive int; clamped to the operator maximum), and ``cursor``
    (an opaque ``next_cursor`` from a prior page). A ``cursor`` pins
    ``start``/``end``/``time_field``/``detail``; supplying any of those with a
    conflicting value is a 400. Results carry a top-level ``next_cursor`` (null
    when the window is exhausted); follow it to page the whole window.

    Returns:
        A ``JsonResponse``: 200 with the page, 400 with a JSON error body on any
        invalid parameter or cursor conflict, or 405 for a non-GET method.
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'method not allowed'}, status=405)

    params = request.GET
    try:
        kwargs: dict = {}
        if 'start' in params:
            kwargs['start'] = _parse_timestamp(params['start'], 'start')
        if 'end' in params:
            kwargs['end'] = _parse_timestamp(params['end'], 'end')
        if 'time_field' in params:
            kwargs['time_field'] = params['time_field']
        if 'detail' in params:
            kwargs['detail'] = params['detail']
        if 'page_size' in params:
            kwargs['page_size'] = _parse_page_size(params['page_size'])
        if 'cursor' in params:
            kwargs['cursor'] = params['cursor']

        result = recent_crossmatches(**kwargs)
    except InvalidQuery as exc:
        logger.info('recent_crossmatches bad request', error=str(exc))
        return JsonResponse({'error': str(exc)}, status=400)

    return JsonResponse(result)


def openapi_view(request: HttpRequest) -> JsonResponse:
    """GET the OpenAPI 3.1 document, built from live settings (R22, KTD14).

    Returns:
        A ``JsonResponse``: 200 with the document, or a structured 405 error for
        a non-GET method.
    """
    if request.method != 'GET':
        return error_response(
            ApiError(
                'method not allowed',
                code=ErrorCode.METHOD_NOT_ALLOWED,
                status=405,
            )
        )
    return JsonResponse(build_document())


def _method_not_allowed() -> ApiError:
    """The structured 405 error."""
    return ApiError(
        'method not allowed', code=ErrorCode.METHOD_NOT_ALLOWED, status=405,
    )


def _reject_json_constant(name: str) -> Any:
    """Refuse ``NaN``/``Infinity``: not JSON, and they could not be echoed back."""
    raise ValueError(f'{name} is not valid JSON')


def _json_body(request: HttpRequest) -> Any:
    """Decode a request's JSON body, or raise ``InvalidQuery`` naming the problem.

    Args:
        request: The request.

    Returns:
        The decoded body.

    Raises:
        InvalidQuery: If the content type is not ``application/json``
            (``param`` ``Content-Type``), or the body is too large or not
            valid JSON (``param`` ``body``).
    """
    if request.content_type != 'application/json':
        raise InvalidQuery(
            f'Content-Type must be application/json, got {request.content_type!r}',
            param='Content-Type',
        )
    try:
        raw = request.body
    except RequestDataTooBig:
        raise InvalidQuery('request body is too large', param='body') from None
    try:
        return json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise InvalidQuery(f'request body is not valid JSON: {exc}', param='body') from None


@api_guard
def get_object_view(request: HttpRequest, object_id: str) -> JsonResponse:
    """GET one Rubin object by ``diaObjectId`` (R1).

    The path segment is the ID as a decimal string; the optional ``detail``
    query param is ``ids`` | ``position`` | ``matches`` (default) | ``full``.

    Returns:
        A ``JsonResponse``: 200 with one result, 400 naming ``diaObjectId`` or
        ``detail``, 405 for a non-GET method, or a guard error (KTD12).
    """
    if request.method != 'GET':
        raise _method_not_allowed()
    result = get_object(dia_object_id=object_id, detail=request.GET.get('detail'))
    return JsonResponse(result)


@api_guard
def lookup_objects_view(request: HttpRequest) -> JsonResponse:
    """POST a batch of tagged inputs; one result per input, in order (R2, R7).

    Read-only and idempotent despite the method: the body carries the input
    list, which can be too long for a query string. The body is a JSON object
    with ``inputs`` and optional ``detail``. A malformed input is reported in
    its own result; request-level problems are a 400 naming the parameter.

    Returns:
        A ``JsonResponse``: 200 with the results, 400 naming the parameter, 405
        for a non-POST method, or a guard error (KTD12).
    """
    if request.method != 'POST':
        raise _method_not_allowed()
    kwargs = parse_lookup_body(_json_body(request))
    return JsonResponse(lookup_objects(**kwargs))
