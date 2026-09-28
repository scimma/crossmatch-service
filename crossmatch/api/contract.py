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

import re
from typing import Any

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
    'searched': 'The catalog was searched at this position.',
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
        parsed = int(value)
    else:
        parsed = None
    if parsed is None or not (0 <= parsed <= _INT64_MAX):
        raise InvalidQuery(
            f'{param} must be a non-negative 64-bit integer or decimal string, '
            f'got {value!r}',
            param=param,
        )
    return parsed


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
    'ReadTimeCatalogOutcome',
    'dia_object_id_fields',
    'envelope',
    'error_response',
    'parse_dia_object_id',
]
