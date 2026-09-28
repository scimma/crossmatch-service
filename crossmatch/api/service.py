"""Read-model query layer for the recent-crossmatch API.

Returns the catalog crossmatches for objects that had an alert in a time
window, grouped by object, at a caller-selected detail level. This module is
the reusable seam other scientist-facing endpoints (object lookup, ranked
transients, cone search) and a future Python client can share; the HTTP view in
``api/views.py`` is a thin adapter over ``recent_crossmatches``.

The query is index-backed on the selected timestamp (``ingest_time`` default,
``event_time`` optional) and semi-joins ``CatalogMatch`` so objects with no
match are excluded (matches-only). The ``full`` detail level reconstructs the
exact published Hopskotch payload via the shared ``build_published_payload``
builder, so it cannot drift from what the crossmatch pipeline publishes.

The endpoint gains the shared response contract additively (R6, R17): the page
carries the service-level ``provenance`` block and an ``as_of`` pin (KTD11),
and every key it had before keeps its type and meaning. Queries run in
``api.guard.sql_phase`` blocks, so the guarded view stays inside the request
budget (KTD12).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from core.log import get_logger
from core.models import Alert, CatalogMatch, TnsAssociation
from matching.payload import build_published_payload
from matching.tns_match import tns_payload

from api.contract import dia_object_id_fields, envelope
# InvalidQuery is defined in api.errors so the cursor codec (api.pagination) can
# raise it without a circular import; re-exported here for existing importers.
from api.errors import InvalidQuery
from api.guard import check_deadline, sql_phase
from api.pagination import Cursor, decode_cursor, encode_cursor, ensure_no_conflict

logger = get_logger(__name__)

DETAIL_LEVELS = ('ids', 'position', 'matches', 'full')
TIME_FIELDS = ('ingest_time', 'event_time')
DEFAULT_DETAIL = 'matches'
DEFAULT_TIME_FIELD = 'ingest_time'
DEFAULT_WINDOW_HOURS = 12

__all__ = ['InvalidQuery', 'recent_crossmatches']


def recent_crossmatches(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    time_field: str | None = None,
    detail: str | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return one keyset page of recent crossmatches grouped by object.

    Results are ordered newest-first by ``time_field`` with ``diaObjectId`` as a
    unique tiebreaker, and paged by opaque cursor: a page carries a
    ``next_cursor`` the caller passes back to resume strictly after the last row.
    Following ``next_cursor`` to exhaustion returns every distinct matched
    ``diaObjectId`` in the window exactly once. There is no total-object cap; only
    per-page size and window span are bounded.

    The first page pins the walk with ``as_of`` (now), an upper bound on
    ``ingest_time`` that its cursors carry, so an alert ingested after
    ``as_of`` never joins it. ``ingest_time`` is set before the ingest commits,
    so an alert whose ingest commits during the walk can still join it if its
    ``ingest_time`` is at or before ``as_of``. A cursor minted before the pin existed continues from its
    position and pins the rest of the walk at that request. The pin fixes which
    alerts are in the set, not their matches: an object in the pinned set whose
    first match lands mid-walk can still appear on a later page (R6).

    Args:
        start: Window start (aware datetime). Defaults to ``end`` minus 12h.
            Ignored (and, if it conflicts, rejected) when ``cursor`` is given.
        end: Window end (aware datetime). Defaults to now. Same cursor rule.
        time_field: Which alert timestamp the window filters on — ``ingest_time``
            (default, arrival time) or ``event_time`` (observation time). Same
            cursor rule.
        detail: Payload detail level — ``ids`` | ``position`` | ``matches``
            (default) | ``full`` (cumulative). Same cursor rule.
        page_size: Caller page size; clamped down to
            ``RECENT_CROSSMATCH_MAX_PAGE_SIZE``. Defaults to
            ``RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE``. Must be positive when given.
        cursor: Opaque ``next_cursor`` from a prior page. It pins
            ``start``/``end``/``time_field``/``detail``; supplying any of those
            with a conflicting value is an error. ``page_size`` is not pinned.

    Returns:
        A JSON-native dict with the service-level ``provenance``, the resolved
        query metadata, the effective ``page_size``, a per-page ``count``, a
        ``next_cursor`` (null when the window is exhausted), the ``as_of`` pin,
        and an ``objects`` list, one entry per ``diaObjectId`` (matches-only).

    Raises:
        InvalidQuery: On an unknown/decoded-invalid ``detail``/``time_field``, a
            non-positive ``page_size``, a malformed ``cursor``, a cursor whose
            pinned context conflicts with an explicit param, an ``end`` before
            ``start``, or a window span beyond the configured maximum.
    """
    # A cursor pins the query context. Decode it first, reject any conflicting
    # explicit param (KTD3), then derive the pinned values. The derived values
    # are then run through the SAME allowlist/window-span validation as
    # directly-supplied params below, so an unsigned (client-constructable)
    # cursor cannot smuggle a bad time_field into the ORM field interpolation.
    # `cursor is not None` (not truthiness): an explicit empty `?cursor=` is a
    # malformed cursor and must reach decode_cursor's 400, not be served as page 1.
    decoded = decode_cursor(cursor) if cursor is not None else None
    if decoded is not None:
        ensure_no_conflict(
            decoded, start=start, end=end, time_field=time_field, detail=detail,
        )
        start, end = decoded.start, decoded.end
        time_field, detail = decoded.time_field, decoded.detail
    # The walk's pin (KTD11): carried by the cursor, or set now on a first page
    # and on a cursor minted before the pin existed.
    as_of = decoded.as_of if decoded is not None and decoded.as_of else timezone.now()

    time_field = time_field if time_field is not None else DEFAULT_TIME_FIELD
    detail = detail if detail is not None else DEFAULT_DETAIL

    if detail not in DETAIL_LEVELS:
        raise InvalidQuery(
            f"detail must be one of {DETAIL_LEVELS}, got {detail!r}",
            param='cursor' if decoded is not None else 'detail',
        )
    if time_field not in TIME_FIELDS:
        raise InvalidQuery(
            f"time_field must be one of {TIME_FIELDS}, got {time_field!r}",
            param='cursor' if decoded is not None else 'time_field',
        )
    if page_size is not None and page_size <= 0:
        raise InvalidQuery("page_size must be a positive integer", param='page_size')

    # Per-page and window-span ceilings, read live (matching the repo convention
    # of reading settings at call time, so @override_settings works in tests).
    # The endpoint is unauthenticated on DEV with rate limiting deferred; these
    # bound the work one request/page can cause. There is intentionally no total
    # cap on how many objects a window can be paged through.
    max_page_size = int(settings.RECENT_CROSSMATCH_MAX_PAGE_SIZE)
    default_page_size = int(settings.RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE)
    max_window_hours = int(settings.RECENT_CROSSMATCH_MAX_WINDOW_HOURS)

    # Clamp both the caller value and the default to the max, so MAX_PAGE_SIZE is
    # a real per-request ceiling even if an operator sets DEFAULT above MAX.
    effective_page_size = min(
        default_page_size if page_size is None else page_size, max_page_size
    )

    end = end or timezone.now()
    start = start or (end - timedelta(hours=DEFAULT_WINDOW_HOURS))
    window_params = ['cursor'] if decoded is not None else ['start', 'end']
    if end < start:
        raise InvalidQuery("end must not be earlier than start", params=window_params)
    if (end - start) > timedelta(hours=max_window_hours):
        raise InvalidQuery(
            f"window span exceeds the maximum of {max_window_hours} hours",
            params=window_params,
        )

    window = {f'{time_field}__gte': start, f'{time_field}__lt': end}

    # Objects (one Alert per diaObjectId, since diaObjectId is unique) that have
    # at least one match, in the window, newest first. An Exists() semi-join
    # expresses "has a match" without the inner-join fan-out (one row per match)
    # that would otherwise need a GROUP BY to collapse, so the planner can serve
    # the ORDER BY + LIMIT straight off the timestamp index. ra/dec are pulled in
    # the same query (functionally dependent on the unique object id) so
    # position/matches/full need no second round trip. The selected time_field is
    # projected too (as the row's t0) so the last kept row can seed next_cursor.
    has_match = CatalogMatch.objects.filter(
        alert_id=OuterRef('lsst_diaObject_diaObjectId')
    )
    query = Alert.objects.filter(**window, ingest_time__lte=as_of).filter(
        Exists(has_match)
    )

    if decoded is not None:
        # Resume strictly after the cursor's (t0, id0) in the descending
        # (time_field DESC, diaObjectId ASC) order: an earlier timestamp, or the
        # same timestamp with a larger id (the tiebreaker arm).
        query = query.filter(
            Q(**{f'{time_field}__lt': decoded.time_field_value})
            | Q(**{
                time_field: decoded.time_field_value,
                'lsst_diaObject_diaObjectId__gt': decoded.dia_object_id,
            })
        )

    # Fetch one extra row to detect whether a further page exists.
    with sql_phase():
        fetched = list(
            query.order_by(f'-{time_field}', 'lsst_diaObject_diaObjectId')
            .values('lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg', time_field)
            [: effective_page_size + 1]
        )
    has_next = len(fetched) > effective_page_size
    object_rows = fetched[:effective_page_size]

    next_cursor = None
    if has_next and object_rows:
        last = object_rows[-1]
        next_cursor = encode_cursor(
            Cursor(
                time_field_value=last[time_field],
                dia_object_id=int(last['lsst_diaObject_diaObjectId']),
                start=start,
                end=end,
                time_field=time_field,
                detail=detail,
                as_of=as_of,
            )
        )

    # Detail levels are cumulative (ids < position < matches/full); each guard
    # skips work, not the shared return.
    objects = [dia_object_id_fields(r['lsst_diaObject_diaObjectId']) for r in object_rows]

    if detail != 'ids' and object_rows:
        for obj, row in zip(objects, object_rows):
            ra, dec = row['ra_deg'], row['dec_deg']
            obj['ra'] = float(ra) if ra is not None else None
            obj['dec'] = float(dec) if dec is not None else None

        if detail != 'position':
            object_ids = [obj['diaObjectId'] for obj in objects]
            with sql_phase():
                matches_by_object = _load_matches(object_ids, detail)
            check_deadline()
            for obj in objects:
                obj['matches'] = matches_by_object.get(obj['diaObjectId'], [])

    return _envelope(
        start, end, time_field, detail, objects, effective_page_size, next_cursor,
        as_of,
    )


def _load_matches(object_ids, detail, *, include_match_version=False):
    """Return {diaObjectId: [match_entry, ...]} for the given objects.

    Postgres ``DISTINCT ON (object, catalog, source)`` combined with an
    ``ORDER BY ... -match_version`` keeps only the current match version per
    match, so a re-matched object does not surface duplicate rows across
    versions and older versions are never fetched. At the ``full`` level each
    entry is the reconstructed published payload; at ``matches`` each entry is
    the catalog/source/separation summary (and the heavier ``catalog_payload``
    column is not loaded).

    Each row is built defensively: an unexpected value in one stored row (e.g. a
    null/non-finite source coordinate on a ``full`` build) is logged and skipped
    without 500-ing the whole response, mirroring the per-row guard on the write
    path in ``tasks/crossmatch.py``.

    With ``include_match_version`` each entry also carries its row's
    ``match_version``, so a caller can join it to the object's crossmatch
    record on ``(alert, match_version)`` (KTD5); the caller owns that key.
    """
    rows = (
        CatalogMatch.objects.filter(alert_id__in=object_ids)
        .order_by('alert_id', 'catalog_name', 'catalog_source_id', '-match_version')
        .distinct('alert_id', 'catalog_name', 'catalog_source_id')
    )
    if detail != 'full':
        rows = rows.only(
            'alert_id', 'catalog_name', 'catalog_source_id', 'match_distance_arcsec',
            'match_version',
        )

    # At the full level, the persisted TNS association (write path: tasks/crossmatch.py)
    # is what carries the tns block into the API — the live snapshot is not consulted
    # here — so both surfaces publish the same enrichment (plan U8/KTD2). One row per
    # alert; loaded once and replicated across that alert's match entries.
    associations: dict[int, TnsAssociation] = {}
    if detail == 'full':
        associations = {
            int(a.alert_id): a
            for a in TnsAssociation.objects.filter(alert_id__in=object_ids)
        }

    result: dict[int, list[dict[str, Any]]] = {}
    for cm in rows:
        try:
            oid = int(cm.alert_id)
            if detail == 'full':
                assoc = associations.get(oid)
                tns = None
                tns_checked = False
                tns_snapshot_epoch = None
                if assoc is not None:
                    tns_checked = assoc.checked
                    tns_snapshot_epoch = assoc.snapshot_epoch
                    if assoc.objid is not None:
                        tns = tns_payload(
                            objid=assoc.objid,
                            name=assoc.name,
                            type=assoc.type,
                            redshift=assoc.redshift,
                            separation_arcsec=assoc.separation_arcsec,
                            url_template=settings.TNS_OBJECT_URL_TEMPLATE,
                        )
                entry = build_published_payload(
                    cm.alert_id,
                    cm.source_ra_deg,
                    cm.source_dec_deg,
                    cm.catalog_name,
                    cm.catalog_source_id,
                    cm.match_distance_arcsec,
                    cm.catalog_payload,
                    tns=tns,
                    tns_checked=tns_checked,
                    tns_snapshot_epoch=tns_snapshot_epoch,
                )
            else:  # 'matches'
                entry = {
                    'catalog_name': cm.catalog_name,
                    'catalog_source_id': cm.catalog_source_id,
                    'separation_arcsec': float(cm.match_distance_arcsec),
                }
        except Exception:
            logger.exception('Skipping unbuildable match row',
                             catalog=getattr(cm, 'catalog_name', None))
            continue
        if include_match_version:
            entry['match_version'] = int(cm.match_version)
        result.setdefault(oid, []).append(entry)
    return result


def _envelope(
    start: datetime,
    end: datetime,
    time_field: str,
    detail: str,
    objects: list[dict[str, Any]],
    page_size: int,
    next_cursor: str | None,
    as_of: datetime,
) -> dict[str, Any]:
    """Wrap the projected objects with the resolved query metadata.

    ``count`` is the number of objects on *this page*, not a whole-set total (no
    cheap total exists under keyset paging). ``next_cursor`` is the opaque token
    to fetch the next page, or ``None`` when the window is exhausted.

    The service-level ``provenance`` block (R17) and ``as_of`` are additions;
    every earlier key keeps its type and meaning (R6).
    """
    return envelope({
        'window': {'start': start.isoformat(), 'end': end.isoformat()},
        'time_field': time_field,
        'detail': detail,
        'page_size': page_size,
        'count': len(objects),
        'next_cursor': next_cursor,
        'objects': objects,
        'as_of': as_of.isoformat(),
    })
