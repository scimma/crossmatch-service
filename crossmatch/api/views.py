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
from django.db import DatabaseError
from django.http import HttpRequest, JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware

from core.log import get_logger
from api.contract import ErrorCode, error_response
from api.discovery import check_database, describe_service, service_status
from api.errors import ApiError
from api.filters import filter_query_params, is_filter_param
from api.guard import api_guard
from api.lookup import get_object, lookup_objects, parse_lookup_body
from api.openapi import build_document
from api.positions import cone_search, resolve_tns
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

    This endpoint does not filter or count (KTD9 step 6): a filter-named
    parameter or ``response`` is a structured ``400 unsupported_parameter``, so
    unfiltered results are never mistaken for filtered ones. Every other
    unknown parameter is ignored, as before.

    Returns:
        A ``JsonResponse``: 200 with the page, 400 with a JSON error body on any
        invalid parameter or cursor conflict, or 405 for a non-GET method.
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'method not allowed'}, status=405)

    params = request.GET
    unsupported = [
        name for name in params if name == 'response' or is_filter_param(name)
    ]
    if unsupported:
        logger.info('recent_crossmatches unsupported parameter', params=unsupported)
        return error_response(ApiError(
            'recent-crossmatches does not support filters or response; use '
            'POST api/lookup, GET api/cone, or GET api/tns/<name> to filter or '
            f'count. Unsupported: {", ".join(unsupported)}',
            code=ErrorCode.UNSUPPORTED_PARAMETER,
            status=400,
            param=unsupported[0] if len(unsupported) == 1 else None,
            params=unsupported if len(unsupported) > 1 else None,
        ))
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
    Filter query params and ``response`` as in ``api.filters``.

    Returns:
        A ``JsonResponse``: 200 with one result, 400 naming ``diaObjectId`` or
        ``detail``, 405 for a non-GET method, or a guard error (KTD12).
    """
    if request.method != 'GET':
        raise _method_not_allowed()
    result = get_object(
        dia_object_id=object_id,
        detail=request.GET.get('detail'),
        filters=filter_query_params(request.GET),
        response=request.GET.get('response'),
    )
    return JsonResponse(result)


@api_guard
def lookup_objects_view(request: HttpRequest) -> JsonResponse:
    """POST a batch of tagged inputs; one result per input, in order (R2, R7).

    Read-only and idempotent despite the method: the body carries the input
    list, which can be too long for a query string. The body is a JSON object
    with ``inputs`` and optional ``detail``, ``radius_arcsec``, ``filters``,
    and ``response``. A malformed input is reported in
    its own result; request-level problems are a 400 naming the parameter.

    Returns:
        A ``JsonResponse``: 200 with the results, 400 naming the parameter, 405
        for a non-POST method, or a guard error (KTD12).
    """
    if request.method != 'POST':
        raise _method_not_allowed()
    kwargs = parse_lookup_body(_json_body(request))
    return JsonResponse(lookup_objects(**kwargs))


@api_guard
def cone_search_view(request: HttpRequest) -> JsonResponse:
    """GET one page of the Rubin objects within a radius of a position (R4, R16).

    Query params: ``ra`` and ``dec`` (degrees) and ``radius_arcsec`` (arcsec,
    at most ``API_MAX_CONE_RADIUS_ARCSEC``), required unless ``cursor`` is
    given; optional ``detail``, ``page_size``, ``cursor`` (a prior page's
    ``next_cursor``, which pins the query and its ``as_of``), filter params,
    and ``response``.

    Returns:
        A ``JsonResponse``: 200 with the page, 400 naming the parameter, 405
        for a non-GET method, or a guard error (KTD12).
    """
    if request.method != 'GET':
        raise _method_not_allowed()
    params = request.GET
    result = cone_search(
        ra=params.get('ra'),
        dec=params.get('dec'),
        radius_arcsec=params.get('radius_arcsec'),
        detail=params.get('detail'),
        page_size=params.get('page_size'),
        cursor=params.get('cursor'),
        filters=filter_query_params(params),
        response=params.get('response'),
    )
    return JsonResponse(result)


@api_guard
def resolve_tns_view(request: HttpRequest, name: str) -> JsonResponse:
    """GET the Rubin objects around a TNS object, by TNS name (R3, R21, R31).

    The path segment is the name (``2026abc``, ``SN 2026abc``, ...); optional
    query params ``radius_arcsec``, ``detail``, filter params, and ``response``.

    Returns:
        A ``JsonResponse``: 200 with one result (including
        ``resolver_unavailable`` and ``tns_name_not_found``), 400 naming the
        parameter, 405 for a non-GET method, or a guard error (KTD12).
    """
    if request.method != 'GET':
        raise _method_not_allowed()
    result = resolve_tns(
        name=name,
        radius_arcsec=request.GET.get('radius_arcsec'),
        detail=request.GET.get('detail'),
        filters=filter_query_params(request.GET),
        response=request.GET.get('response'),
    )
    return JsonResponse(result)


@api_guard(map_unavailable=False)
def _checked_status(request: HttpRequest) -> JsonResponse:
    """The status body after the database checks pass, under the request guard."""
    epoch = check_database()
    return JsonResponse(service_status(database_ok=True, snapshot_epoch=epoch))


def service_status_view(request: HttpRequest) -> JsonResponse:
    """GET service availability, version, and TNS-resolution availability (R30).

    Always 200 for a GET (KTD16): a database that cannot be reached, fails, or
    does not answer the short status check in time is reported as ``database:
    unavailable`` (with ``tns_resolution: unavailable``), never as a 503, so
    this endpoint is exempt from the guard's outage mapping (KTD12 step 6).
    ``/healthz``, the pod probe, is separate and unchanged.

    Returns:
        A ``JsonResponse``: 200 with the status body, or a structured 405 error
        for a non-GET method.
    """
    if request.method != 'GET':
        return error_response(_method_not_allowed())
    try:
        response = _checked_status(request)
    except DatabaseError as exc:
        logger.warning('service_status database unavailable', error=str(exc))
        response = None
    if response is None or response.status_code != 200:
        # The guard answers an over-budget check with a 400; for status, a
        # database too slow to answer a trivial query is unavailable.
        response = JsonResponse(service_status(database_ok=False, snapshot_epoch=None))
    return response


def describe_service_view(request: HttpRequest) -> JsonResponse:
    """GET the catalogs, vocabulary, and per-request limits of the service (KTD16).

    Built from live settings with no database access, so it answers even when
    the database is down, and it never opens a LSDB/HATS catalog.

    Returns:
        A ``JsonResponse``: 200 with the description, or a structured 405 error
        for a non-GET method.
    """
    if request.method != 'GET':
        return error_response(_method_not_allowed())
    return JsonResponse(describe_service())
