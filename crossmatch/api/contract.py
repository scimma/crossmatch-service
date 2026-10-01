"""The API response contract: codes, envelope, errors, and 64-bit IDs (KTD2, KTD4).

Codes are fixed snake_case values that agents branch on; a rename would break
them, so the values never change once published. What each code means lives in
``CODE_DESCRIPTIONS``, which the OpenAPI document (``api/openapi.py``) renders
into its enum descriptions.

Stored codes (per-catalog search outcomes, broker-cut ``enforced_by`` and
status) are owned by ``core.models`` because the crossmatch task writes them;
this module reuses them and adds the codes that exist only in responses.
"""

from __future__ import annotations

import math
import re
from typing import Any

from django.conf import settings
from django.db import models
from django.http import JsonResponse

from api.errors import ApiError, InvalidQuery
from core import provenance
from core.models import CatalogSearchOutcome, CutEnforcedBy, CutStatus

CONTRACT_VERSION = provenance.CONTRACT_VERSION


class ObjectStatus(models.TextChoices):
    """What the service knows about one Rubin object (R8)."""
    NOT_IN_SERVICE = 'not_in_service'
    CROSSMATCH_PENDING = 'crossmatch_pending'
    COINCIDENT_SOURCES = 'coincident_sources'
    NO_COINCIDENT_SOURCE = 'no_coincident_source'
    NOT_SEARCHED = 'not_searched'


class InputStatus(models.TextChoices):
    """The outcome of one request input (an ID, a position, or a TNS name)."""
    NO_RUBIN_OBJECT = 'no_rubin_object'
    OBJECTS_FOUND = 'objects_found'
    TNS_NAME_NOT_FOUND = 'tns_name_not_found'
    RESOLVER_UNAVAILABLE = 'resolver_unavailable'
    INVALID_INPUT = 'invalid_input'


class ReadTimeCatalogOutcome(models.TextChoices):
    """Per-catalog outcomes known only when a response is built (KTD5)."""
    NOT_RECORDED = 'not_recorded'
    NOT_IN_SERVICE_AT_CROSSMATCH = 'not_in_service_at_crossmatch'


class ErrorCode(models.TextChoices):
    """Request-level error codes (R28, R29)."""
    INVALID_PARAMETER = 'invalid_parameter'
    METHOD_NOT_ALLOWED = 'method_not_allowed'
    QUERY_TOO_EXPENSIVE = 'query_too_expensive'
    SERVICE_UNAVAILABLE = 'service_unavailable'
    FILTERS_SPAN_CATALOGS = 'filters_span_catalogs'
    UNSUPPORTED_PARAMETER = 'unsupported_parameter'


class QualifiesReason(models.TextChoices):
    """Why an object or input does or does not qualify under filters (R14; KTD9).

    Reasons that restate a status reuse that status's code.
    """
    MEETS_FILTERS = 'meets_filters'
    OBJECT_FILTERS_NOT_MET = 'object_filters_not_met'
    NO_SOURCE_MEETS_FILTERS = 'no_source_meets_filters'
    NO_OBJECT_MEETS_FILTERS = 'no_object_meets_filters'
    NOT_IN_SERVICE = 'not_in_service'
    CROSSMATCH_PENDING = 'crossmatch_pending'
    NO_COINCIDENT_SOURCE = 'no_coincident_source'
    NOT_SEARCHED = 'not_searched'
    NO_RUBIN_OBJECT = 'no_rubin_object'
    TNS_NAME_NOT_FOUND = 'tns_name_not_found'
    RESOLVER_UNAVAILABLE = 'resolver_unavailable'
    INVALID_INPUT = 'invalid_input'


# Every per-catalog outcome a response can carry: the stored ones first, then
# the read-time ones.
CATALOG_OUTCOME_VALUES: list[str] = (
    CatalogSearchOutcome.values + ReadTimeCatalogOutcome.values
)

CODE_DESCRIPTIONS: dict[str, str] = {
    # ObjectStatus (R8, R9)
    'not_in_service': (
        'The service has no record of this object. This is not evidence that '
        'the object failed a reliability cut or does not exist in Rubin: '
        'reliability filtering happens before alerts reach the service.'
    ),
    'crossmatch_pending': 'Ingested, not yet crossmatched.',
    'coincident_sources': (
        'Crossmatched; at least one catalog source lies within the crossmatch '
        'radius. A coincident source is not a host association.'
    ),
    'no_coincident_source': (
        'Crossmatched; no source within the crossmatch radius in any catalog '
        'that was actually searched. Not evidence of a hostless transient.'
    ),
    'not_searched': (
        'Crossmatched, but no catalog was actually searched (every catalog was '
        'skipped, outside its footprint, or not searched because of an invalid '
        'position).'
    ),
    # InputStatus
    'no_rubin_object': 'No Rubin object in this service lies within the radius.',
    'objects_found': 'One or more Rubin objects matched this input.',
    'tns_name_not_found': (
        'The TNS name is not in the current TNS snapshot (the snapshot epoch is '
        'reported).'
    ),
    'resolver_unavailable': (
        'TNS-name resolution is unavailable right now; other inputs are '
        'unaffected.'
    ),
    'invalid_input': (
        'This input is malformed; it is reported here and does not fail the '
        'other inputs.'
    ),
    # CatalogSearchOutcome (stored)
    'searched': (
        'The catalog was searched at this position: the position was inside the '
        'catalog footprint at the resolution of its HATS coverage map (see '
        'coverage_map in api/describe), so an object in a footprint hole or near '
        'an edge may be recorded as searched even though the catalog has no data '
        'there.'
    ),
    'outside_footprint': 'The position is outside the catalog footprint.',
    'skipped_read_failure': 'The catalog was skipped after a read failure.',
    'not_searched_invalid_position': (
        'Not searched because the object position is invalid.'
    ),
    # ReadTimeCatalogOutcome
    'not_recorded': (
        'No search outcome is recorded for this object; the release that '
        'started recording is named.'
    ),
    'not_in_service_at_crossmatch': (
        'The catalog was added after this object was crossmatched.'
    ),
    # ErrorCode
    'invalid_parameter': 'A request parameter is invalid; see param or params.',
    'method_not_allowed': 'The HTTP method is not supported on this path.',
    'query_too_expensive': (
        'The request exceeded its server-side cost bound. Narrow it; retrying '
        'the same request will not help.'
    ),
    'service_unavailable': (
        'The service or its database is unavailable. Retry after the '
        'Retry-After interval.'
    ),
    'filters_span_catalogs': (
        'The match filters name more than one catalog. Match filters apply '
        "within one catalog's source; params lists the conflicting parameters."
    ),
    'unsupported_parameter': (
        'This operation does not support the named parameter (for example a '
        'filter or response on recent-crossmatches); the request was not run, '
        'so unfiltered results are never mistaken for filtered ones.'
    ),
    # QualifiesReason (only the codes that are not also a status)
    'meets_filters': 'Qualifies: meets every filter in the request.',
    'object_filters_not_met': (
        'Does not qualify: the object fails an object filter (reliability); an '
        'object with no stored reliability never meets one.'
    ),
    'no_source_meets_filters': (
        'Does not qualify: the object has coincident sources, but no single '
        'current source satisfies every match filter. A null or non-numeric '
        'stored value never satisfies a filter.'
    ),
    'no_object_meets_filters': (
        'Does not qualify: Rubin objects lie within the radius, but none '
        'qualifies.'
    ),
    # CutEnforcedBy (stored)
    'service': 'Enforced by this service.',
    'broker': 'Enforced by the broker before alerts reach this service.',
    # CutStatus (stored)
    'service_setting': "The value is this service's own live setting.",
    'declared': (
        'The value is declared by the maintainer as of the stated date; the '
        'service does not observe it.'
    ),
    'not_declared': 'The maintainer has not declared a value.',
}

def envelope(body: dict[str, Any]) -> dict[str, Any]:
    """Wrap a response body with live service-level provenance (R17).

    Args:
        body: The operation's JSON-native response fields.

    Returns:
        ``{'provenance': ..., **body}``.

    Raises:
        ValueError: If ``body`` already has a ``provenance`` key.
    """
    if 'provenance' in body:
        raise ValueError("response body already has a 'provenance' key")
    return {'provenance': provenance.service_provenance(), **body}


def error_response(err: ApiError) -> JsonResponse:
    """Render an ``ApiError`` as its JSON error response.

    Args:
        err: The error.

    Returns:
        A ``JsonResponse`` with the error's status and body, plus
        ``Retry-After`` when the error sets one.
    """
    resp = JsonResponse(err.to_dict(), status=err.status)
    if err.retry_after is not None:
        resp['Retry-After'] = str(int(err.retry_after))
    return resp


_INT64_MAX = 2**63 - 1
_DECIMAL = re.compile(r'[0-9]+')


def parse_dia_object_id(value: Any, param: str = 'diaObjectId') -> int:
    """Parse a ``diaObjectId`` given as a JSON integer or a decimal string (KTD4).

    Strings let JavaScript clients pass IDs above 2^53 without precision loss.
    Floats and booleans are rejected rather than coerced, since a float may
    already have lost precision.

    Args:
        value: The raw value.
        param: The parameter name reported in the error.

    Returns:
        The ID as an int in [0, 2^63 - 1].

    Raises:
        InvalidQuery: If the value is not a non-negative int64 integer or
            decimal-digit string.
    """
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _DECIMAL.fullmatch(value):
        try:
            parsed = int(value)
        except ValueError:  # past Python's int-string digit limit
            parsed = None
    else:
        parsed = None
    if parsed is None or not (0 <= parsed <= _INT64_MAX):
        raise InvalidQuery(
            f'{param} must be a non-negative 64-bit integer or decimal string, '
            f'got {value!r}',
            param=param,
        )
    return parsed


def _parse_number(value: Any, param: str, allow_string: bool) -> float:
    """Parse a finite number: a JSON number, or a decimal string when allowed.

    Raises:
        InvalidQuery: If the value is missing, a boolean, not a number (or
            numeric string when allowed), or not finite.
    """
    parsed: float | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, (int, float)):
        parsed = float(value)
    elif allow_string and isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            parsed = None
    if parsed is None or not math.isfinite(parsed):
        kind = 'a finite number' if not allow_string else 'a finite decimal number'
        raise InvalidQuery(f'{param} must be {kind}, got {value!r}', param=param)
    return parsed


def parse_position(
    ra: Any,
    dec: Any,
    *,
    ra_param: str = 'ra',
    dec_param: str = 'dec',
    allow_string: bool = False,
) -> tuple[float, float]:
    """Parse a sky position in degrees (ICRS RA in [0, 360], Dec in [-90, 90]).

    Args:
        ra: Right ascension, degrees.
        dec: Declination, degrees.
        ra_param: The name reported for an invalid ``ra``.
        dec_param: The name reported for an invalid ``dec``.
        allow_string: Accept decimal strings (query-string parameters).

    Returns:
        ``(ra, dec)`` as floats.

    Raises:
        InvalidQuery: If either value is missing, not a finite number, or out
            of range; ``param`` names it.
    """
    if ra is None:
        raise InvalidQuery(f'{ra_param} is required', param=ra_param)
    ra_deg = _parse_number(ra, ra_param, allow_string)
    if not 0.0 <= ra_deg <= 360.0:
        raise InvalidQuery(
            f'{ra_param} must be in [0, 360] degrees, got {ra!r}', param=ra_param
        )
    if dec is None:
        raise InvalidQuery(f'{dec_param} is required', param=dec_param)
    dec_deg = _parse_number(dec, dec_param, allow_string)
    if not -90.0 <= dec_deg <= 90.0:
        raise InvalidQuery(
            f'{dec_param} must be in [-90, 90] degrees, got {dec!r}', param=dec_param
        )
    return ra_deg, dec_deg


def parse_radius_arcsec(
    value: Any, *, param: str = 'radius_arcsec', allow_string: bool = False,
) -> float:
    """Parse a search radius in arcsec, bounded by ``API_MAX_CONE_RADIUS_ARCSEC``.

    The maximum is read at call time, so an environment override needs only a
    restart (KTD12).

    Args:
        value: The radius, arcsec.
        param: The name reported when it is invalid.
        allow_string: Accept a decimal string (query-string parameters).

    Returns:
        The radius as a float in (0, maximum].

    Raises:
        InvalidQuery: If the radius is missing, not a finite number, not
            positive, or above the maximum.
    """
    if value is None:
        raise InvalidQuery(f'{param} is required', param=param)
    radius = _parse_number(value, param, allow_string)
    maximum = float(settings.API_MAX_CONE_RADIUS_ARCSEC)
    if not 0.0 < radius <= maximum:
        raise InvalidQuery(
            f'{param} must be greater than 0 and at most {maximum:g} arcsec, '
            f'got {value!r}',
            param=param,
        )
    return radius


def dia_object_id_fields(object_id: int) -> dict[str, Any]:
    """The integer and string forms of a ``diaObjectId`` for a response (KTD4).

    Args:
        object_id: The ID.

    Returns:
        ``{'diaObjectId': int, 'diaObjectId_str': str}``.
    """
    object_id = int(object_id)
    return {'diaObjectId': object_id, 'diaObjectId_str': str(object_id)}


__all__ = [
    'CATALOG_OUTCOME_VALUES',
    'CODE_DESCRIPTIONS',
    'CONTRACT_VERSION',
    'ApiError',
    'CatalogSearchOutcome',
    'CutEnforcedBy',
    'CutStatus',
    'ErrorCode',
    'InputStatus',
    'InvalidQuery',
    'ObjectStatus',
    'QualifiesReason',
    'ReadTimeCatalogOutcome',
    'dia_object_id_fields',
    'envelope',
    'error_response',
    'parse_dia_object_id',
    'parse_position',
    'parse_radius_arcsec',
]
