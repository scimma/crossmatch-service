"""Position and TNS-name inputs: cone searches over every object the service has seen.

Covers R3, R4, R5, R7, R8, R12, R16, R21, R31 and KTD11-KTD13.

* ``cone_search`` answers one position (``GET api/cone``), paged in
  ``(ingest_time, diaObjectId)`` order under an ``as_of`` pin (KTD11).
* ``resolve_tns`` answers one TNS name (``GET api/tns/<name>``): the name is
  resolved through the TNS snapshot and answered as a position search around
  the TNS position, echoing the TNS record and the snapshot epoch.
* The ``position`` and ``tns`` input kinds of ``POST api/lookup``, registered
  in ``api.lookup.INPUT_KINDS``. They share one resolver, so every cone in a
  request, whichever kind produced it, runs in one statement and spends the
  per-request object budget in input order.

Searches reach the whole archive (R12), not only crossmatched objects: an
object that has not been crossmatched yet is listed with status
``crossmatch_pending``. Objects whose ``healpix_ipix`` is null (an invalid
position at ingest) cannot be found by position.

Every cone is covered by at most nine coarse HEALPix pixels widened to order-16
ranges (``core.healpix.cone_cover_ranges``), and one statement joins the list
of ``(input index, lo, hi)`` ranges to ``core_alert`` with the exact
haversine separation filter in SQL, so totals and caps count objects inside the
radius, not cover candidates (KTD12 step 5). A batched position or a TNS
lookup lists at most ``API_MAX_OBJECTS_PER_POSITION`` objects, with
``truncated`` and the ``total`` when capped; a single cone pages instead.

With filters or ``response=count`` (``api.filters``) every object within the
radius is found, not only the listed ones: filters mark the listed objects and
report ``qualifying_total`` over all of them, and a count covers all of them,
uncapped by the listing limits.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models.functions import Lower
from django.utils import timezone

from api.contract import (
    InputStatus,
    parse_position,
    parse_radius_arcsec,
)
from api.errors import InvalidQuery
from api.filters import (
    RESPONSE_COUNT,
    FilterSpec,
    parse_filters,
    parse_response_mode,
)
from api.guard import check_deadline, sql_phase
from api.lookup import (
    INPUT_KINDS,
    InputError,
    InputKind,
    LookupContext,
    ParsedInput,
    _answer,
    _check_detail,
    _context,
    build_objects,
    finish_response,
)
from api.pagination import ConeCursor, decode_cone_cursor, encode_cone_cursor
from core.healpix import cone_cover_ranges
from core.models import Alert, TnsObject, TnsSnapshotMeta
from matching.payload import _to_json_scalar
from matching.tns_match import tns_payload

#: ``truncation`` of a result whose objects were capped.
TRUNCATION_PER_INPUT = 'per_input_limit'
TRUNCATION_PER_REQUEST = 'per_request_limit'
TRUNCATION_VALUES = (TRUNCATION_PER_INPUT, TRUNCATION_PER_REQUEST)

#: Resolver group shared by the position and TNS kinds.
CONE_GROUP = 'cone'

_TNS_PREFIX = re.compile(r'^(?:sn|at)', re.IGNORECASE)
_TNS_NAME = re.compile(r'[0-9]{4}[0-9a-z]+')
_TNS_NAME_MAX_LENGTH = 64

# Great-circle separation of core_alert row ``a`` from cone ``c``, in arcsec
# (the haversine form, stable at small separations, as in
# core.healpix.angular_separation_arcsec).
_SEPARATION_SQL = (
    '2.0 * 3600.0 * degrees(asin(least(1.0, sqrt('
    'power(sin(radians(a.dec_deg - c.dec) / 2.0), 2) '
    '+ cos(radians(c.dec)) * cos(radians(a.dec_deg)) '
    '* power(sin(radians(a.ra_deg - c.ra) / 2.0), 2)))))'
)


@dataclass(frozen=True)
class Cone:
    """One cone to search.

    Attributes:
        key: The caller's key for the cone (the input index).
        ra: Center RA, degrees.
        dec: Center Dec, degrees.
        radius_arcsec: Radius, arcsec.
    """

    key: int
    ra: float
    dec: float
    radius_arcsec: float


@dataclass
class ConeHits:
    """What one cone found.

    Attributes:
        total: Objects inside the radius (under ``as_of`` when pinned).
        rows: ``(diaObjectId, ingest_time, separation_arcsec)`` of the listed
            objects, in ``(ingest_time, diaObjectId)`` order.
    """

    total: int = 0
    rows: list[tuple[int, datetime, float]] = field(default_factory=list)


def search_cones(
    cones: Sequence[Cone],
    *,
    per_cone_limit: int | None = None,
    as_of: datetime | None = None,
    after: tuple[datetime, int] | None = None,
    limit: int | None = None,
) -> dict[int, ConeHits]:
    """Find the Rubin objects inside each cone, in one statement.

    Args:
        cones: The cones; keys must be distinct.
        per_cone_limit: List at most this many objects per cone (the first in
            ``(ingest_time, diaObjectId)`` order).
        as_of: Only objects ingested at or before this time.
        after: Only objects strictly after this ``(ingest_time,
            diaObjectId)`` keyset position (single-cone paging).
        limit: List at most this many objects in all.

    Returns:
        ``{cone key: ConeHits}`` for every cone with at least one listed
        object. ``total`` counts every object inside the radius regardless of
        ``per_cone_limit``, ``after``, and ``limit``.
    """
    cone_keys, ras, decs, radii = [], [], [], []
    range_keys, los, his = [], [], []
    for cone in cones:
        check_deadline()
        cone_keys.append(cone.key)
        ras.append(cone.ra)
        decs.append(cone.dec)
        radii.append(cone.radius_arcsec)
        for lo, hi in cone_cover_ranges(cone.ra, cone.dec, cone.radius_arcsec):
            range_keys.append(cone.key)
            los.append(lo)
            his.append(hi)
    if not cones:
        return {}

    table = Alert._meta.db_table
    params: list[Any] = [cone_keys, ras, decs, radii, range_keys, los, his]
    as_of_sql = ''
    if as_of is not None:
        as_of_sql = 'AND a.ingest_time <= %s'
        params.append(as_of)
    filters = []
    if per_cone_limit is not None:
        filters.append('rn <= %s')
        params.append(int(per_cone_limit))
    if after is not None:
        filters.append('(ingest_time, oid) > (%s, %s)')
        params.extend([after[0], int(after[1])])
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ''
    limit_sql = ''
    if limit is not None:
        limit_sql = 'LIMIT %s'
        params.append(int(limit))

    # Cover ranges of one cone never overlap (they are distinct coarse pixels),
    # so an object joins each cone at most once and needs no DISTINCT.
    sql = f'''
        WITH cones AS (
            SELECT * FROM unnest(
                %s::integer[], %s::double precision[], %s::double precision[],
                %s::double precision[]
            ) AS c(idx, ra, dec, radius)
        ), cover AS (
            SELECT * FROM unnest(%s::integer[], %s::bigint[], %s::bigint[])
                AS r(idx, lo, hi)
        ), inside AS (
            SELECT idx, oid, ingest_time, sep FROM (
                SELECT c.idx, a.lsst_diaobject_diaobjectid AS oid,
                       a.ingest_time, {_SEPARATION_SQL} AS sep, c.radius
                FROM cover r
                JOIN cones c ON c.idx = r.idx
                JOIN {table} a ON a.healpix_ipix BETWEEN r.lo AND r.hi
                WHERE TRUE {as_of_sql}
            ) candidates
            WHERE sep <= radius
        ), ranked AS (
            SELECT idx, oid, ingest_time, sep,
                   count(*) OVER (PARTITION BY idx) AS total,
                   row_number() OVER (
                       PARTITION BY idx ORDER BY ingest_time, oid
                   ) AS rn
            FROM inside
        )
        SELECT idx, oid, ingest_time, sep, total FROM ranked
        {where_sql}
        ORDER BY idx, ingest_time, oid
        {limit_sql}
    '''
    hits: dict[int, ConeHits] = {}
    with sql_phase():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    for key, object_id, ingest_time, separation, total in rows:
        cone_hits = hits.setdefault(int(key), ConeHits(total=int(total)))
        cone_hits.rows.append((int(object_id), ingest_time, float(separation)))
    return hits


def normalize_tns_name(value: Any) -> str:
    """Normalize a TNS name to its lowercased bare designation (KTD13).

    Whitespace and a leading ``SN`` or ``AT`` prefix are removed and the rest
    is lowercased, so ``SN 2026abc``, ``AT2026abc``, and ``2026ABC`` all give
    ``2026abc``. Stored names are compared lowercased, so ``2026a`` finds the
    IAU designation ``2026A``.

    Args:
        value: The name as given.

    Returns:
        The normalized designation: four year digits and a suffix.

    Raises:
        ValueError: If the value is not a string, is too long, or does not
            normalize to a designation.
    """
    if not isinstance(value, str):
        raise ValueError(f'name must be a string, got {value!r}')
    if len(value) > _TNS_NAME_MAX_LENGTH:
        raise ValueError(f'name is longer than {_TNS_NAME_MAX_LENGTH} characters')
    name = _TNS_PREFIX.sub('', ''.join(value.split()), count=1).lower()
    if not _TNS_NAME.fullmatch(name):
        raise ValueError(
            f'name must be a TNS designation such as "2026abc" or "SN 2026abc", '
            f'got {value!r}'
        )
    return name


@dataclass(frozen=True)
class TnsQuery:
    """A parsed TNS input: the normalized name and the search radius (arcsec)."""

    name: str
    radius_arcsec: float


def _parse_tns(
    name: Any,
    radius: Any,
    ctx: LookupContext,
    *,
    name_param: str,
    radius_param: str,
    allow_string: bool,
) -> tuple[TnsQuery, dict[str, Any]]:
    """Validate a TNS name and radius (the input's, else the shared, else TNS's).

    Raises:
        InvalidQuery: If either is invalid; ``param`` names it.
    """
    try:
        normalized = normalize_tns_name(name)
    except ValueError as exc:
        raise InvalidQuery(str(exc), param=name_param) from None
    if radius is None:
        radius_arcsec = (
            ctx.radius_arcsec if ctx.radius_arcsec is not None
            else float(settings.TNS_MATCH_RADIUS_ARCSEC)
        )
    else:
        radius_arcsec = parse_radius_arcsec(
            radius, param=radius_param, allow_string=allow_string
        )
    query = TnsQuery(name=normalized, radius_arcsec=radius_arcsec)
    return query, {'kind': TnsInput.name, 'name': normalized, 'radius_arcsec': radius_arcsec}


class _ConeKind(InputKind):
    """Base of the kinds answered as cone searches; they share one resolver."""

    max_setting = 'API_MAX_POSITIONS'
    group = CONE_GROUP

    def resolve(
        self, entries: list[ParsedInput], ctx: LookupContext,
    ) -> dict[int, dict[str, Any]]:
        """Resolve every position and TNS input of the request together."""
        return _resolve_cone_inputs(entries, ctx)


class PositionInput(_ConeKind):
    """``{"kind": "position", "ra": <deg>, "dec": <deg>, "radius_arcsec": <arcsec>}`` (R4, R5).

    ``radius_arcsec`` may be omitted when the request carries a shared one.
    """

    name = 'position'
    fields = ('ra', 'dec', 'radius_arcsec')

    def parse(
        self, raw: dict[str, Any], param: str, ctx: LookupContext,
    ) -> tuple[Any, dict[str, Any]]:
        """Parse the position and radius; see ``InputKind.parse``."""
        try:
            ra, dec = parse_position(
                raw.get('ra'), raw.get('dec'),
                ra_param=f'{param}.ra', dec_param=f'{param}.dec',
            )
            if 'radius_arcsec' in raw or ctx.radius_arcsec is None:
                radius = parse_radius_arcsec(
                    raw.get('radius_arcsec'), param=f'{param}.radius_arcsec'
                )
            else:
                radius = ctx.radius_arcsec
        except InvalidQuery as exc:
            raise InputError(exc.message, exc.param or param) from None
        normalized = {'kind': self.name, 'ra': ra, 'dec': dec, 'radius_arcsec': radius}
        return (ra, dec, radius), normalized


class TnsInput(_ConeKind):
    """``{"kind": "tns", "name": <TNS name>, "radius_arcsec": <arcsec>}`` (R3).

    ``radius_arcsec`` is optional: the request's shared radius, else
    ``TNS_MATCH_RADIUS_ARCSEC``.
    """

    name = 'tns'
    fields = ('name', 'radius_arcsec')

    def parse(
        self, raw: dict[str, Any], param: str, ctx: LookupContext,
    ) -> tuple[Any, dict[str, Any]]:
        """Parse the name and radius; see ``InputKind.parse``."""
        try:
            if 'name' not in raw:
                raise InvalidQuery('name is required', param=f'{param}.name')
            return _parse_tns(
                raw['name'], raw.get('radius_arcsec'), ctx,
                name_param=f'{param}.name', radius_param=f'{param}.radius_arcsec',
                allow_string=False,
            )
        except InvalidQuery as exc:
            raise InputError(exc.message, exc.param or param) from None


INPUT_KINDS[PositionInput.name] = PositionInput()
INPUT_KINDS[TnsInput.name] = TnsInput()


def _iso(value: datetime | None) -> str | None:
    """An aware datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None


def _tns_record(obj: TnsObject) -> dict[str, Any]:
    """The TNS record echoed with a resolved name (R3)."""
    record = tns_payload(
        objid=int(obj.objid), name=obj.name, type=obj.type,
        redshift=_to_json_scalar(obj.redshift), separation_arcsec=None,
        url_template=settings.TNS_OBJECT_URL_TEMPLATE,
    )
    del record['separation_arcsec']
    record['name_prefix'] = obj.name_prefix
    record['ra'] = _to_json_scalar(obj.ra_deg)
    record['dec'] = _to_json_scalar(obj.dec_deg)
    return record


def _lookup_tns_names(names: set[str]) -> tuple[datetime | None, dict[str, TnsObject]]:
    """Resolve normalized names through a current TNS snapshot (KTD13).

    There is no fallback to stored ``TnsAssociation`` rows: without a current
    snapshot the resolver is unavailable.

    Returns:
        ``(epoch, {normalized name: TnsObject})``; ``epoch`` is ``None`` (and
        the map empty) when the snapshot is absent or stale.
    """
    with sql_phase():
        epoch = TnsSnapshotMeta.current_epoch()
        if epoch is None:
            return None, {}
        found: dict[str, TnsObject] = {}
        for obj in (
            TnsObject.objects.annotate(name_lower=Lower('name'))
            .filter(name_lower__in=sorted(names))
            .order_by('objid')
        ):
            found.setdefault(obj.name_lower, obj)
    return epoch, found


def _object_entries(
    rows: list[tuple[int, datetime, float]], objects: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Object entries of one cone, each with its separation from the center."""
    return [
        {**objects[object_id], 'separation_arcsec': separation}
        for object_id, _ingest_time, separation in rows
    ]


def _resolve_cone_inputs(
    entries: list[ParsedInput], ctx: LookupContext,
) -> dict[int, dict[str, Any]]:
    """Answer every position and TNS input of a request (R3, R4, R5, R7).

    TNS names are resolved first; every resulting cone then runs in one
    statement, each capped at ``API_MAX_OBJECTS_PER_POSITION``. The remaining
    per-request object budget is spent in input order: an input past it keeps
    its status and total and lists what fits, marked ``per_request_limit``.

    When the context needs candidates (filters or a count), the statement is
    not capped: every object ID within each radius is recorded in
    ``ctx.candidates``, and a count lists every object, ignoring the caps.
    """
    results: dict[int, dict[str, Any]] = {}
    extra: dict[int, dict[str, Any]] = {}
    cones: list[Cone] = []

    tns_entries = [entry for entry in entries if entry.kind == TnsInput.name]
    if tns_entries:
        epoch, records = _lookup_tns_names({entry.value.name for entry in tns_entries})
        for entry in tns_entries:
            record = records.get(entry.value.name)
            if epoch is None or record is None:
                status = (
                    InputStatus.RESOLVER_UNAVAILABLE if epoch is None
                    else InputStatus.TNS_NAME_NOT_FOUND
                )
                results[entry.index] = {
                    'status': status.value,
                    'objects': [],
                    'total': None,
                    'truncated': False,
                    'truncation': None,
                    'tns': None,
                    'tns_snapshot_epoch': _iso(epoch),
                }
                continue
            extra[entry.index] = {
                'tns': _tns_record(record),
                'tns_snapshot_epoch': _iso(epoch),
            }
            cones.append(Cone(
                entry.index, float(record.ra_deg), float(record.dec_deg),
                entry.value.radius_arcsec,
            ))
    for entry in entries:
        if entry.kind == PositionInput.name:
            cones.append(Cone(entry.index, *entry.value))
    cones.sort(key=lambda cone: cone.key)

    per_input = int(settings.API_MAX_OBJECTS_PER_POSITION)
    hits = search_cones(
        cones, per_cone_limit=None if ctx.needs_candidates else per_input
    )

    kept: dict[int, tuple[ConeHits, list[tuple[int, datetime, float]], str | None]] = {}
    for cone in cones:
        check_deadline()
        cone_hits = hits.get(cone.key, ConeHits())
        rows = cone_hits.rows
        if ctx.needs_candidates:
            ctx.candidates[cone.key] = [row[0] for row in rows]
        if ctx.count_only:
            kept[cone.key] = (cone_hits, rows, None)
            continue
        rows = rows[:per_input]
        truncation = TRUNCATION_PER_INPUT if cone_hits.total > len(rows) else None
        if len(rows) > ctx.object_budget:
            rows = rows[:ctx.object_budget]
            truncation = TRUNCATION_PER_REQUEST
            ctx.truncated = True
        ctx.object_budget -= len(rows)
        kept[cone.key] = (cone_hits, rows, truncation)

    objects = build_objects(
        [row[0] for _hits, rows, _t in kept.values() for row in rows], ctx
    )
    for key, (cone_hits, rows, truncation) in kept.items():
        results[key] = {
            'status': (
                InputStatus.OBJECTS_FOUND if cone_hits.total
                else InputStatus.NO_RUBIN_OBJECT
            ).value,
            'objects': _object_entries(rows, objects),
            'total': cone_hits.total,
            'truncated': truncation is not None,
            'truncation': truncation,
            **extra.get(key, {}),
        }
    return results


def _parse_page_size(value: Any) -> int:
    """The cone page size: default ``API_MAX_OBJECTS_PER_POSITION``, clamped
    to ``API_MAX_OBJECTS_PER_REQUEST``.

    Raises:
        InvalidQuery: If the value is not a positive integer (``param``
            ``page_size``).
    """
    maximum = int(settings.API_MAX_OBJECTS_PER_REQUEST)
    if value is None:
        return min(int(settings.API_MAX_OBJECTS_PER_POSITION), maximum)
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        parsed = None
    if parsed is None or parsed <= 0:
        raise InvalidQuery(
            f'page_size must be a positive integer, got {value!r}', param='page_size'
        )
    return min(parsed, maximum)


def _pinned(given: Any, pinned: Any, param: str, parse) -> None:
    """Reject an explicit parameter that differs from the cursor's pinned value.

    Raises:
        InvalidQuery: If ``given`` is set and parses to another value.
    """
    if given is not None and parse(given) != pinned:
        raise InvalidQuery(f"{param} conflicts with the cursor's pinned query", param=param)


def cone_search(
    *,
    ra: Any = None,
    dec: Any = None,
    radius_arcsec: Any = None,
    detail: str | None = None,
    page_size: Any = None,
    cursor: str | None = None,
    filters: Any = None,
    response: Any = None,
) -> dict[str, Any]:
    """Search one position for every Rubin object within the radius (R4, R16).

    The first page pins the object set with ``as_of`` (now); following
    ``next_cursor`` pages the rest in ``(ingest_time, diaObjectId)`` order
    without an object ingested mid-walk appearing. An object's status can
    still advance between pages as crossmatching proceeds (KTD11).

    Args:
        ra: Center RA in degrees, a number or decimal string.
        dec: Center Dec in degrees, a number or decimal string.
        radius_arcsec: Radius in arcsec, at most ``API_MAX_CONE_RADIUS_ARCSEC``.
        detail: ``ids`` | ``position`` | ``matches`` (default) | ``full``.
        page_size: Objects per page (default ``API_MAX_OBJECTS_PER_POSITION``,
            clamped to ``API_MAX_OBJECTS_PER_REQUEST``); not pinned.
        cursor: ``next_cursor`` of a prior page. It pins ``ra``, ``dec``,
            ``radius_arcsec``, ``detail``, and ``as_of``; an explicit
            conflicting value is an error.
        filters: ``{filter name: value}`` from the query string. Not pinned by
            the cursor (the listed set does not depend on them): pass them on
            every page.
        response: ``objects`` (default) or ``count``. A count covers every
            object within the radius now and takes no ``cursor``.

    Returns:
        The lookup-shaped response with one position result, plus ``as_of``,
        ``page_size``, ``next_cursor`` (null on the last page), and
        ``filters`` when filtered; or the counts body for ``response=count``.

    Raises:
        InvalidQuery: If a parameter or the cursor is invalid or conflicts with
            the cursor; ``param`` names it.
        ApiError: ``filters_span_catalogs``.
    """
    spec = parse_filters(filters, allow_string=True)
    mode = parse_response_mode(response)
    if mode == RESPONSE_COUNT:
        if cursor is not None:
            raise InvalidQuery(
                'cursor pages a listing; response=count counts the whole set and '
                'takes no cursor',
                param='cursor',
            )
        return _count_cone(ra, dec, radius_arcsec, detail, spec, mode)
    decoded: ConeCursor | None = None
    if cursor is not None:
        decoded = decode_cone_cursor(cursor)
        _pinned(ra, decoded.ra, 'ra',
                lambda v: parse_position(v, 0, allow_string=True)[0])
        _pinned(dec, decoded.dec, 'dec',
                lambda v: parse_position(0, v, allow_string=True)[1])
        _pinned(radius_arcsec, decoded.radius_arcsec, 'radius_arcsec',
                lambda v: parse_radius_arcsec(v, allow_string=True))
        _pinned(detail, decoded.detail, 'detail', _check_detail)
        center = parse_position(decoded.ra, decoded.dec)
        radius = parse_radius_arcsec(decoded.radius_arcsec)
        detail = decoded.detail
        as_of = decoded.as_of
        after = (decoded.ingest_time, decoded.dia_object_id)
    else:
        center = parse_position(ra, dec, allow_string=True)
        radius = parse_radius_arcsec(radius_arcsec, allow_string=True)
        as_of = timezone.now()
        after = None
    detail = _check_detail(detail)
    size = _parse_page_size(page_size)

    cone = Cone(0, center[0], center[1], radius)
    cone_hits = search_cones([cone], as_of=as_of, after=after, limit=size + 1).get(0)
    rows = cone_hits.rows[:size] if cone_hits is not None else []
    has_next = cone_hits is not None and len(cone_hits.rows) > size
    if cone_hits is not None:
        total = cone_hits.total
    elif after is not None:
        # Past the last object: the total comes from the pinned set as a whole.
        whole = search_cones([cone], as_of=as_of, limit=1).get(0)
        total = whole.total if whole is not None else 0
    else:
        total = 0

    ctx = _context(detail, spec, mode)
    if spec is not None:
        # qualifying_total covers the whole pinned set, not only this page.
        whole = search_cones([cone], as_of=as_of).get(0)
        ctx.candidates[0] = [row[0] for row in whole.rows] if whole is not None else []
    objects = build_objects([row[0] for row in rows], ctx)

    next_cursor = None
    if has_next:
        last_id, last_time, _separation = rows[-1]
        next_cursor = encode_cone_cursor(ConeCursor(
            ingest_time=last_time, dia_object_id=last_id, as_of=as_of,
            ra=center[0], dec=center[1], radius_arcsec=radius, detail=detail,
        ))

    echoed = {
        name: value for name, value in (
            ('ra', ra), ('dec', dec), ('radius_arcsec', radius_arcsec),
            ('cursor', cursor),
        ) if value is not None
    }
    result = _cone_result(echoed, center, radius, total, _object_entries(rows, objects))
    return finish_response([result], ctx, {
        'detail': detail,
        'count': 1,
        'provenance_sets': ctx.provenance_sets,
        'truncated': False,
        'results': [result],
        'as_of': as_of.isoformat(),
        'page_size': size,
        'next_cursor': next_cursor,
    })


def _cone_result(
    echoed: dict[str, Any],
    center: tuple[float, float],
    radius: float,
    total: int,
    objects: list[dict[str, Any]],
) -> dict[str, Any]:
    """The one position result of a single cone search."""
    return {
        'index': 0,
        'kind': PositionInput.name,
        'input': echoed,
        'normalized': {
            'kind': PositionInput.name, 'ra': center[0], 'dec': center[1],
            'radius_arcsec': radius,
        },
        'status': (
            InputStatus.OBJECTS_FOUND if total else InputStatus.NO_RUBIN_OBJECT
        ).value,
        'objects': objects,
        'total': total,
        'truncated': False,
        'truncation': None,
    }


def _count_cone(
    ra: Any,
    dec: Any,
    radius_arcsec: Any,
    detail: Any,
    spec: FilterSpec | None,
    mode: str,
) -> dict[str, Any]:
    """``response=count`` of a single cone: every object within the radius now.

    ``page_size`` does not apply to a count and is ignored.
    """
    center = parse_position(ra, dec, allow_string=True)
    radius = parse_radius_arcsec(radius_arcsec, allow_string=True)
    _check_detail(detail)
    cone_hits = search_cones([Cone(0, center[0], center[1], radius)]).get(0)
    rows = cone_hits.rows if cone_hits is not None else []
    ctx = _context('ids', spec, mode)
    objects = build_objects([row[0] for row in rows], ctx)
    echoed = {
        name: value for name, value in (
            ('ra', ra), ('dec', dec), ('radius_arcsec', radius_arcsec),
        ) if value is not None
    }
    result = _cone_result(echoed, center, radius, len(rows), _object_entries(rows, objects))
    return finish_response([result], ctx, {})


def resolve_tns(
    *,
    name: Any,
    radius_arcsec: Any = None,
    detail: str | None = None,
    filters: Any = None,
    response: Any = None,
) -> dict[str, Any]:
    """Look up a TNS name and answer as a position search around it (R3, R21, R31).

    Not paged: the result lists at most ``API_MAX_OBJECTS_PER_POSITION``
    objects, with ``truncated`` and the ``total`` when capped; a cone search
    at the echoed TNS position pages the rest (KTD11).

    Args:
        name: The TNS name, e.g. ``SN 2026abc`` or ``2026abc``.
        radius_arcsec: Radius in arcsec (number or decimal string); default
            ``TNS_MATCH_RADIUS_ARCSEC``, at most ``API_MAX_CONE_RADIUS_ARCSEC``.
        detail: As for ``lookup_objects``.
        filters: ``{filter name: value}`` from the query string.
        response: ``objects`` (default) or ``count``.

    Returns:
        The lookup-shaped response with one TNS result, whose ``input`` echoes
        ``name`` as given; or the counts body for ``response=count``.

    Raises:
        InvalidQuery: If the name, radius, detail, a filter, or ``response``
            is invalid; ``param`` names it.
        ApiError: ``filters_span_catalogs``.
    """
    detail = _check_detail(detail)
    ctx = _context(
        detail, parse_filters(filters, allow_string=True), parse_response_mode(response),
    )
    value, normalized = _parse_tns(
        name, radius_arcsec, ctx,
        name_param='name', radius_param='radius_arcsec', allow_string=True,
    )
    entry = ParsedInput(
        index=0, raw=name, kind=TnsInput.name, value=value, normalized=normalized,
    )
    return _answer([entry], ctx)
