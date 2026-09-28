"""Opaque keyset cursor codec for the paged read-model queries.

Two kinds of cursor share one codec, told apart by a ``kind`` discriminator
(KTD11): ``recent_crossmatches`` (:class:`Cursor`) and ``cone_search``
(:class:`ConeCursor`). A cursor minted before the discriminator existed carries
no ``kind`` and decodes as ``recent_crossmatches``; one with no ``as_of`` pin
decodes as unpinned, so those cursors keep working. A cursor presented to the
other query is rejected naming ``cursor``.

The cone cursor also pins its object set with ``as_of``, an upper bound on
``ingest_time`` (set once when an alert is ingested and never changed), so an
object ingested while a caller walks the pages never appears mid-walk.

The recent-crossmatches cursor is described below.

A page's ``next_cursor`` names the last row of that page as a keyset position
``(time_field_value, dia_object_id)`` and pins the query context the cursor was
issued for (``start``/``end``/``time_field``/``detail``). The service resumes the
next page strictly after that position (see ``api/service.py``).

The token is ``base64url(compact JSON)`` and **unsigned**: it encodes only public
query parameters and a public keyset position, so a tampered cursor yields at
most a different *valid* public query the client could have issued directly.
There is no trust boundary to protect here (the endpoint is unauthenticated), but
the service still routes the decoded ``time_field``/``detail``/window through the
same allowlist and window-span validation as directly-supplied params before
using them, so a decoded value never reaches the ORM unchecked.

Timestamps round-trip as full-precision ISO-8601 so the ``=`` arm of the keyset
predicate (``time_field == t0``) holds exactly against the ``timestamptz``
microsecond resolution stored in Postgres.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware

from api.errors import InvalidQuery

# Compact JSON keys keep the token short. The wire shape is an implementation
# detail; clients treat the whole cursor as opaque.
_KEY_TIME_VALUE = 't'
_KEY_OBJECT_ID = 'i'
_KEY_START = 's'
_KEY_END = 'e'
_KEY_TIME_FIELD = 'f'
_KEY_DETAIL = 'd'
_KEY_KIND = 'k'
_KEY_AS_OF = 'a'
_KEY_RA = 'ra'
_KEY_DEC = 'dec'
_KEY_RADIUS = 'r'

#: Cursor kinds (KTD11).
KIND_RECENT_CROSSMATCHES = 'recent_crossmatches'
KIND_CONE_SEARCH = 'cone_search'
_KINDS = (KIND_RECENT_CROSSMATCHES, KIND_CONE_SEARCH)

# Upper bound on an accepted cursor string. A real cursor encodes six short
# fields (two/four ISO timestamps, an int64, two allowlisted keywords) and comes
# in well under this; the cap only rejects oversized garbage cheaply.
_MAX_CURSOR_LENGTH = 1024


@dataclass(frozen=True)
class Cursor:
    """A decoded keyset cursor: a position plus the pinned query context."""

    time_field_value: datetime
    dia_object_id: int
    start: datetime
    end: datetime
    time_field: str
    detail: str
    kind: str = KIND_RECENT_CROSSMATCHES
    as_of: datetime | None = None


@dataclass(frozen=True)
class ConeCursor:
    """A decoded cone-search cursor: a keyset position plus the pinned query.

    Attributes:
        ingest_time: ``ingest_time`` of the last object on the page.
        dia_object_id: ``diaObjectId`` of the last object on the page.
        as_of: The pinned upper bound on ``ingest_time``.
        ra: Cone-center RA, degrees.
        dec: Cone-center Dec, degrees.
        radius_arcsec: Cone radius, arcsec.
        detail: The detail level.
    """

    ingest_time: datetime
    dia_object_id: int
    as_of: datetime
    ra: float
    dec: float
    radius_arcsec: float
    detail: str


def _encode(payload: dict) -> str:
    """``base64url`` of compact JSON, without ``=`` padding."""
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _decode_payload(raw: str, kind: str) -> dict:
    """Decode a token to its JSON object and check it is of ``kind``.

    Raises:
        InvalidQuery: If the token is empty, too long, not base64url JSON
            object, of an unknown kind, or of another kind than ``kind``.
    """
    if not raw:
        raise InvalidQuery('cursor must not be empty', param='cursor')
    # A legitimate cursor is a compact JSON blob (a few hundred base64 chars).
    # Reject anything far larger up front so an unauthenticated caller cannot
    # force a base64-decode + JSON-parse of an arbitrarily long query-string
    # value that is guaranteed to fail validation anyway.
    if len(raw) > _MAX_CURSOR_LENGTH:
        raise InvalidQuery('cursor is too long', param='cursor')
    try:
        padded = raw + '=' * (-len(raw) % 4)
        data = base64.urlsafe_b64decode(padded.encode('ascii'))
        payload = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise InvalidQuery(
            f'cursor is not a valid token: {raw!r}', param='cursor'
        ) from exc

    if not isinstance(payload, dict):
        raise InvalidQuery('cursor is not a valid token', param='cursor')
    # No kind: minted before the discriminator existed, by recent-crossmatches.
    found = payload.get(_KEY_KIND, KIND_RECENT_CROSSMATCHES)
    if found not in _KINDS:
        raise InvalidQuery('cursor has an unknown kind', param='cursor')
    if found != kind:
        raise InvalidQuery(
            f'cursor was issued by {found}, not {kind}', param='cursor'
        )
    return payload


def encode_cursor(cursor: Cursor) -> str:
    """Serialize a :class:`Cursor` to an opaque URL-safe token.

    Args:
        cursor: The keyset position and pinned query context to encode.

    Returns:
        A ``base64url``-encoded compact-JSON string with no ``=`` padding, safe to
        pass as a bare query-string value.
    """
    payload = {
        _KEY_KIND: cursor.kind,
        _KEY_TIME_VALUE: cursor.time_field_value.isoformat(),
        _KEY_OBJECT_ID: cursor.dia_object_id,
        _KEY_START: cursor.start.isoformat(),
        _KEY_END: cursor.end.isoformat(),
        _KEY_TIME_FIELD: cursor.time_field,
        _KEY_DETAIL: cursor.detail,
    }
    if cursor.as_of is not None:
        payload[_KEY_AS_OF] = cursor.as_of.isoformat()
    return _encode(payload)


def decode_cursor(raw: str) -> Cursor:
    """Parse an opaque token back into a :class:`Cursor`.

    Args:
        raw: The token produced by :func:`encode_cursor`.

    Returns:
        The decoded cursor.

    Raises:
        InvalidQuery: If the token is empty, not valid base64url, not JSON, not
            a recent-crossmatches cursor, is missing a required key, or carries
            an unparseable/ill-typed value.
    """
    payload = _decode_payload(raw, KIND_RECENT_CROSSMATCHES)

    try:
        time_value = payload[_KEY_TIME_VALUE]
        object_id = payload[_KEY_OBJECT_ID]
        start = payload[_KEY_START]
        end = payload[_KEY_END]
        time_field = payload[_KEY_TIME_FIELD]
        detail = payload[_KEY_DETAIL]
    except (KeyError, TypeError) as exc:
        raise InvalidQuery(
            'cursor is missing a required field', param='cursor'
        ) from exc

    if not isinstance(object_id, int) or isinstance(object_id, bool):
        raise InvalidQuery('cursor object id must be an integer', param='cursor')
    if not isinstance(time_field, str) or not isinstance(detail, str):
        raise InvalidQuery(
            'cursor time_field/detail must be strings', param='cursor'
        )

    as_of = payload.get(_KEY_AS_OF)
    try:
        return Cursor(
            time_field_value=_parse_dt(time_value),
            dia_object_id=object_id,
            start=_parse_dt(start),
            end=_parse_dt(end),
            time_field=time_field,
            detail=detail,
            as_of=_parse_dt(as_of) if as_of is not None else None,
        )
    except InvalidQuery as exc:
        raise InvalidQuery(exc.message, param='cursor') from None


def encode_cone_cursor(cursor: ConeCursor) -> str:
    """Serialize a :class:`ConeCursor` to an opaque URL-safe token.

    Args:
        cursor: The keyset position and pinned cone query.

    Returns:
        The token.
    """
    return _encode({
        _KEY_KIND: KIND_CONE_SEARCH,
        _KEY_TIME_VALUE: cursor.ingest_time.isoformat(),
        _KEY_OBJECT_ID: cursor.dia_object_id,
        _KEY_AS_OF: cursor.as_of.isoformat(),
        _KEY_RA: cursor.ra,
        _KEY_DEC: cursor.dec,
        _KEY_RADIUS: cursor.radius_arcsec,
        _KEY_DETAIL: cursor.detail,
    })


def decode_cone_cursor(raw: str) -> ConeCursor:
    """Parse an opaque token back into a :class:`ConeCursor`.

    The decoded query values are not range-checked here; the cone service runs
    them through the same validation as directly supplied parameters.

    Args:
        raw: The token produced by :func:`encode_cone_cursor`.

    Returns:
        The decoded cursor.

    Raises:
        InvalidQuery: If the token is malformed, not a cone-search cursor,
            lacks its ``as_of`` pin or another required key, or carries an
            ill-typed value (``param`` ``cursor``).
    """
    payload = _decode_payload(raw, KIND_CONE_SEARCH)
    try:
        time_value = payload[_KEY_TIME_VALUE]
        object_id = payload[_KEY_OBJECT_ID]
        as_of = payload[_KEY_AS_OF]
        ra = payload[_KEY_RA]
        dec = payload[_KEY_DEC]
        radius = payload[_KEY_RADIUS]
        detail = payload[_KEY_DETAIL]
    except KeyError as exc:
        raise InvalidQuery(
            'cursor is missing a required field', param='cursor'
        ) from exc
    if not isinstance(object_id, int) or isinstance(object_id, bool):
        raise InvalidQuery('cursor object id must be an integer', param='cursor')
    for value in (ra, dec, radius):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise InvalidQuery('cursor position must be numeric', param='cursor')
    if not isinstance(detail, str):
        raise InvalidQuery('cursor detail must be a string', param='cursor')
    try:
        return ConeCursor(
            ingest_time=_parse_dt(time_value),
            dia_object_id=object_id,
            as_of=_parse_dt(as_of),
            ra=float(ra),
            dec=float(dec),
            radius_arcsec=float(radius),
            detail=detail,
        )
    except InvalidQuery as exc:
        raise InvalidQuery(exc.message, param='cursor') from None


def ensure_no_conflict(
    cursor: Cursor,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    time_field: str | None = None,
    detail: str | None = None,
) -> None:
    """Reject an explicit param that conflicts with the cursor's pinned context.

    The cursor is authoritative for ``{start, end, time_field, detail}`` (KTD3):
    a caller may omit them (the service derives them from the cursor) or repeat
    the matching value, but supplying a *different* value means the query drifted
    mid-iteration and is an error. ``page_size`` is intentionally not pinned --
    it is presentation, not result-set identity, and may vary per page.

    Raises:
        InvalidQuery: If any supplied param differs from the cursor's value.
    """
    if start is not None and start != cursor.start:
        raise InvalidQuery(
            "start conflicts with the cursor's pinned window", param='start'
        )
    if end is not None and end != cursor.end:
        raise InvalidQuery("end conflicts with the cursor's pinned window", param='end')
    if time_field is not None and time_field != cursor.time_field:
        raise InvalidQuery(
            "time_field conflicts with the cursor's pinned context", param='time_field'
        )
    if detail is not None and detail != cursor.detail:
        raise InvalidQuery(
            "detail conflicts with the cursor's pinned context", param='detail'
        )


def _parse_dt(value: object) -> datetime:
    """Parse an ISO-8601 timestamp string from a cursor payload.

    Raises:
        InvalidQuery: If ``value`` is not a string parseable as an ISO-8601
            datetime.
    """
    if not isinstance(value, str):
        raise InvalidQuery('cursor timestamp must be a string')
    parsed = parse_datetime(value)
    if parsed is None:
        raise InvalidQuery(f'cursor timestamp is not valid ISO-8601: {value!r}')
    # Server-issued cursors always carry aware UTC timestamps, but a hand-crafted
    # cursor could omit the offset. Coerce naive -> UTC (as the view does for
    # start/end) so downstream `end < start` comparisons and ORM filters never mix
    # naive and aware datetimes (which would raise TypeError -> unhandled 500).
    if is_naive(parsed):
        parsed = make_aware(parsed, timezone.utc)
    return parsed
