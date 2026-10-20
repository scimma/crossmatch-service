"""The window summary: how many Rubin objects arrived over a night or span.

One service shared by ``GET api/summary`` and the MCP tool (KTD1), so names,
defaults, caps, spellings, and error codes cannot drift between them.

This module has two layers:

* **Parse** (``parse_summary_params``): raw request values plus a ``now``
  become a frozen ``SummaryQuery`` (window, filters, ``top_n``). Pure, no
  database, settings read at call time.
* **Aggregate**: computes the statistics for a ``SummaryQuery``.

Night arithmetic is UTC-only (KTD2). A Rubin night is named by its dayObs, the
UTC date of a time minus 12 hours, so night D spans ``[D 12:00Z, D+1 12:00Z)``.
Night D has begun once the clock reaches ``D 00:00Z`` plus
``SUMMARY_NIGHT_ROLLOVER_HOURS`` (default 23, so ``D 23:00Z``, when the
evening's observing starts). With no window the summary covers the latest
begun night, the UTC date of ``now`` minus the rollover hours.

The window is given in exactly one of three forms (KTD3): ``night``
(``YYYY-MM-DD``), ``last_nights`` (the N nights ending at the default night),
or ``start`` + ``end`` (ISO-8601, naive read as UTC, half-open). A span counts
every night it touches toward ``SUMMARY_MAX_NIGHTS``, and the edge nights it
cuts are flagged partial. No window may reach past the latest begun night.
Errors reuse the contract's codes (KTD12): ``invalid_parameter`` naming the
parameter(s), and ``filters_span_catalogs`` for more than one catalog.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db import connection
from django.db.models import Max
from django.utils import timezone as django_timezone

from api.contract import (
    CatalogSearchOutcome,
    ErrorCode,
    ObjectStatus,
    ReadTimeCatalogOutcome,
    SummaryCatalogOutcome,
    dia_object_id_fields,
    envelope,
)
from api.errors import ApiError, InvalidQuery
from api.filters import CATALOG_FILTER
from api.guard import check_deadline, sql_phase
from api.timeparse import parse_timestamp
from core.log import get_logger
from core.models import Alert, AlertDelivery, CatalogMatch, ObjectCrossmatchRecord
from core.provenance import reliability_cuts

logger = get_logger(__name__)

# Window parameters, in the order an error lists them.
NIGHT_PARAM = 'night'
LAST_NIGHTS_PARAM = 'last_nights'
START_PARAM = 'start'
END_PARAM = 'end'
WINDOW_PARAMS = (NIGHT_PARAM, LAST_NIGHTS_PARAM, START_PARAM, END_PARAM)

BROKER_PARAM = 'broker'
CATALOG_PARAM = CATALOG_FILTER
CATALOG_MATCH_PARAM = 'catalog_match'
TOP_N_PARAM = 'top_n'

# Window forms echoed in the answer.
FORM_DEFAULT = 'default'
FORM_NIGHT = 'night'
FORM_LAST_NIGHTS = 'last_nights'
FORM_SPAN = 'span'

CATALOG_MATCH_WITH = 'with'
CATALOG_MATCH_WITHOUT = 'without'
CATALOG_MATCH_VALUES = (CATALOG_MATCH_WITH, CATALOG_MATCH_WITHOUT)

# A dayObs night starts this long after its date's UTC midnight.
_NIGHT_OFFSET = timedelta(hours=12)
_ONE_DAY = timedelta(days=1)
_NIGHT_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
_INTEGER_RE = re.compile(r'\d+')
_NOT_ALPHANUMERIC_RE = re.compile(r'[^a-z0-9]')


@dataclass(frozen=True)
class SummaryWindow:
    """A validated summary window.

    Attributes:
        form: How the caller gave it: ``default``, ``night``, ``last_nights``,
            or ``span``.
        nights: The dayObs dates it covers, oldest first.
        start: Inclusive UTC lower bound on observation time.
        end: Exclusive UTC upper bound on observation time.
        first_partial: The span starts after the first night's start.
        last_partial: The span ends before the last night's end.
    """

    form: str
    nights: tuple[date, ...]
    start: datetime
    end: datetime
    first_partial: bool = False
    last_partial: bool = False

    def is_partial(self, night: date) -> bool:
        """Whether the window cuts ``night`` at either edge."""
        return (self.first_partial and night == self.nights[0]) or (
            self.last_partial and night == self.nights[-1]
        )


@dataclass(frozen=True)
class SummaryFilters:
    """Validated summary filters; ``None`` means not filtered.

    Attributes:
        broker: Normalized broker name (``antares``, ``lasair``, ``pittgoogle``).
        catalog: A ``CROSSMATCH_CATALOGS`` name.
        catalog_match: ``with`` or ``without``; set exactly when ``catalog`` is.
    """

    broker: str | None = None
    catalog: str | None = None
    catalog_match: str | None = None


@dataclass(frozen=True)
class SummaryQuery:
    """A fully validated summary request.

    Attributes:
        window: The window to summarize.
        filters: The filters restricting every statistic.
        top_n: How many most-reliable objects to list.
    """

    window: SummaryWindow
    filters: SummaryFilters
    top_n: int


def day_obs(moment: datetime) -> date:
    """The dayObs of a moment: the UTC date of ``moment`` minus 12 hours.

    Args:
        moment: An aware datetime.

    Returns:
        The Rubin night it falls in.
    """
    return (moment.astimezone(timezone.utc) - _NIGHT_OFFSET).date()


def night_start(night: date) -> datetime:
    """The start of night ``night``: ``night`` 12:00Z."""
    return datetime.combine(night, time(), tzinfo=timezone.utc) + _NIGHT_OFFSET


def night_end(night: date) -> datetime:
    """The exclusive end of night ``night``: the next day 12:00Z."""
    return night_start(night) + _ONE_DAY


def _rollover() -> timedelta:
    return timedelta(hours=int(settings.SUMMARY_NIGHT_ROLLOVER_HOURS))


def has_begun(night: date, now: datetime) -> bool:
    """Whether observing for ``night`` has begun at ``now`` (KTD2).

    Args:
        night: A dayObs date.
        now: The aware query moment.

    Returns:
        ``True`` once ``now`` reaches ``night`` 00:00Z plus
        ``SUMMARY_NIGHT_ROLLOVER_HOURS``.
    """
    midnight = datetime.combine(night, time(), tzinfo=timezone.utc)
    return now >= midnight + _rollover()


def default_night(now: datetime) -> date:
    """The latest begun night at ``now`` (R5, KTD2).

    Args:
        now: The aware query moment.

    Returns:
        The UTC date of ``now`` minus ``SUMMARY_NIGHT_ROLLOVER_HOURS``.
    """
    return (now.astimezone(timezone.utc) - _rollover()).date()


def max_nights() -> int:
    """The most Rubin nights one summary may cover (``SUMMARY_MAX_NIGHTS``)."""
    return int(settings.SUMMARY_MAX_NIGHTS)


def _nights(first: date, last: date) -> tuple[date, ...]:
    return tuple(first + timedelta(days=i) for i in range((last - first).days + 1))


def _values(params: Mapping[str, Any], name: str) -> list[Any]:
    """Every value given for ``name``: ``getlist`` when the mapping has it."""
    getlist = getattr(params, 'getlist', None)
    if callable(getlist):
        return list(getlist(name))
    value = params.get(name)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _parse_int(raw: Any, name: str, maximum: int) -> int:
    """An integer from 1 to ``maximum``, from an int or a decimal string.

    Raises:
        InvalidQuery: For any other value (``param`` ``name``).
    """
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str) and _INTEGER_RE.fullmatch(raw.strip()):
        value = int(raw.strip())
    else:
        value = None
    if value is None or not 1 <= value <= maximum:
        raise InvalidQuery(
            f'{name} must be an integer from 1 to {maximum}, got {raw!r}', param=name
        )
    return value


def _parse_night(raw: Any, now: datetime) -> SummaryWindow:
    """The ``night`` form: one named dayObs that has begun."""
    try:
        if not isinstance(raw, str) or not _NIGHT_RE.fullmatch(raw.strip()):
            raise ValueError(raw)
        night = date.fromisoformat(raw.strip())
    except ValueError:
        raise InvalidQuery(
            f'night must be a dayObs date YYYY-MM-DD, got {raw!r}', param=NIGHT_PARAM
        ) from None
    if not has_begun(night, now):
        raise InvalidQuery(
            f'night {night.isoformat()} has not begun observing; the latest '
            f'begun Rubin night is {default_night(now).isoformat()}',
            param=NIGHT_PARAM,
        )
    return SummaryWindow(
        form=FORM_NIGHT, nights=(night,), start=night_start(night), end=night_end(night)
    )


def _parse_last_nights(raw: Any, now: datetime) -> SummaryWindow:
    """The ``last_nights`` form: N nights ending at the default night."""
    count = _parse_int(raw, LAST_NIGHTS_PARAM, max_nights())
    last = default_night(now)
    first = last - timedelta(days=count - 1)
    return SummaryWindow(
        form=FORM_LAST_NIGHTS,
        nights=_nights(first, last),
        start=night_start(first),
        end=night_end(last),
    )


def _parse_span(raw_start: Any, raw_end: Any, now: datetime) -> SummaryWindow:
    """The ``start`` + ``end`` form: a half-open UTC span."""
    span_params = [START_PARAM, END_PARAM]
    try:
        start = parse_timestamp(raw_start, START_PARAM).astimezone(timezone.utc)
        end = parse_timestamp(raw_end, END_PARAM).astimezone(timezone.utc)
        first = day_obs(start)
        # Half-open: the last instant inside the span is just before ``end``.
        last = day_obs(end - timedelta(microseconds=1))
        night_start(first)
        night_end(last)
    except OverflowError:
        raise InvalidQuery(
            'start and end must lie within the supported date range',
            params=span_params,
        ) from None
    if end <= start:
        raise InvalidQuery('end must be after start', params=span_params)
    touched = (last - first).days + 1
    maximum = max_nights()
    if touched > maximum:
        raise InvalidQuery(
            f'start to end touches {touched} Rubin nights; the maximum is '
            f'{maximum} nights per request',
            params=span_params,
        )
    latest = default_night(now)
    if end > night_end(latest):
        raise InvalidQuery(
            f'end must not be after {night_end(latest).isoformat()}, the end of '
            f'the latest begun Rubin night ({latest.isoformat()})',
            param=END_PARAM,
        )
    return SummaryWindow(
        form=FORM_SPAN,
        nights=_nights(first, last),
        start=start,
        end=end,
        first_partial=start != night_start(first),
        last_partial=end != night_end(last),
    )


def parse_window(params: Mapping[str, Any], now: datetime) -> SummaryWindow:
    """The window the request names, or the default night (R2, R5; KTD2, KTD3).

    Args:
        params: The request parameters (see ``parse_summary_params``).
        now: The aware query moment.

    Returns:
        The validated window.

    Raises:
        InvalidQuery: More than one window form, ``start`` without ``end`` (or
            the reverse), a malformed value, more than ``SUMMARY_MAX_NIGHTS``
            nights, a night not yet begun, or a span ending after the latest
            begun night.
    """
    given = [name for name in WINDOW_PARAMS if params.get(name) is not None]
    forms = {FORM_SPAN if name in (START_PARAM, END_PARAM) else name for name in given}
    if len(forms) > 1:
        raise InvalidQuery(
            'give one window: night, last_nights, or start and end; got '
            f'{", ".join(given)}',
            params=given,
        )
    if not given:
        night = default_night(now)
        return SummaryWindow(
            form=FORM_DEFAULT,
            nights=(night,),
            start=night_start(night),
            end=night_end(night),
        )
    if NIGHT_PARAM in given:
        return _parse_night(params.get(NIGHT_PARAM), now)
    if LAST_NIGHTS_PARAM in given:
        return _parse_last_nights(params.get(LAST_NIGHTS_PARAM), now)
    if len(given) != 2:
        raise InvalidQuery(
            'start and end must be given together', params=[START_PARAM, END_PARAM]
        )
    return _parse_span(params.get(START_PARAM), params.get(END_PARAM), now)


def broker_names() -> list[str]:
    """The broker identifiers, in reporting order (``core/provenance.py``)."""
    return [cut['broker'] for cut in reliability_cuts()]


def catalog_names() -> list[str]:
    """The configured catalog names, in ``CROSSMATCH_CATALOGS`` order."""
    return [str(cat['name']) for cat in settings.CROSSMATCH_CATALOGS]


#: Every parameter the summary accepts, in a stable order.
SUMMARY_PARAMS = (
    *WINDOW_PARAMS,
    BROKER_PARAM,
    CATALOG_PARAM,
    CATALOG_MATCH_PARAM,
    TOP_N_PARAM,
)


def _parse_broker(raw: Any) -> str:
    """A broker name, lowercased with non-alphanumerics removed (KTD8).

    Raises:
        InvalidQuery: If it names no known broker (``param`` ``broker``).
    """
    brokers = broker_names()
    normalized = (
        _NOT_ALPHANUMERIC_RE.sub('', raw.lower()) if isinstance(raw, str) else None
    )
    if normalized not in brokers:
        raise InvalidQuery(
            f'broker must be one of {", ".join(brokers)}, got {raw!r}',
            param=BROKER_PARAM,
        )
    return normalized


def parse_filters(params: Mapping[str, Any]) -> SummaryFilters:
    """The broker and catalog filters (R12, R13, R15; KTD7, KTD8).

    Args:
        params: The request parameters (see ``parse_summary_params``).

    Returns:
        The validated filters. ``catalog`` without ``catalog_match`` means
        ``with``.

    Raises:
        InvalidQuery: An unknown broker or catalog, a ``catalog_match`` other
            than ``with``/``without``, or ``catalog_match`` without ``catalog``.
        ApiError: ``filters_span_catalogs`` when ``catalog`` names more than
            one catalog (``params`` has one ``catalog`` entry per value).
    """
    raw_broker = params.get(BROKER_PARAM)
    broker = _parse_broker(raw_broker) if raw_broker is not None else None

    configured = catalog_names()
    raw_catalogs = _values(params, CATALOG_PARAM)
    for value in raw_catalogs:
        if not isinstance(value, str) or value not in configured:
            raise InvalidQuery(
                f'catalog must be one of {configured}, got {value!r}',
                param=CATALOG_PARAM,
            )
    catalogs = list(dict.fromkeys(raw_catalogs))
    if len(catalogs) > 1:
        raise ApiError(
            'a summary filters on one catalog, but catalog names '
            f'{len(catalogs)} catalogs ({", ".join(catalogs)})',
            code=ErrorCode.FILTERS_SPAN_CATALOGS,
            status=400,
            params=[CATALOG_PARAM] * len(raw_catalogs),
        )
    catalog = catalogs[0] if catalogs else None

    raw_match = params.get(CATALOG_MATCH_PARAM)
    catalog_match = None
    if raw_match is not None:
        catalog_match = (
            raw_match.strip().lower() if isinstance(raw_match, str) else None
        )
        if catalog_match not in CATALOG_MATCH_VALUES:
            raise InvalidQuery(
                f'catalog_match must be with or without, got {raw_match!r}',
                param=CATALOG_MATCH_PARAM,
            )
        if catalog is None:
            raise InvalidQuery(
                'catalog_match requires catalog', param=CATALOG_MATCH_PARAM
            )
    elif catalog is not None:
        catalog_match = CATALOG_MATCH_WITH
    return SummaryFilters(broker=broker, catalog=catalog, catalog_match=catalog_match)


def parse_top_n(params: Mapping[str, Any]) -> int:
    """``top_n``: 1 to ``MCP_MAX_OBJECTS``, default ``SUMMARY_DEFAULT_TOP_N``.

    Raises:
        InvalidQuery: Out of range or not an integer (``param`` ``top_n``).
    """
    raw = params.get(TOP_N_PARAM)
    if raw is None:
        return int(settings.SUMMARY_DEFAULT_TOP_N)
    return _parse_int(raw, TOP_N_PARAM, int(settings.MCP_MAX_OBJECTS))


def parse_summary_params(params: Mapping[str, Any], *, now: datetime) -> SummaryQuery:
    """Validate a summary request's raw parameters.

    Args:
        params: The raw parameters: a ``QueryDict`` (``request.GET``; a repeated
            ``catalog`` is read with ``getlist``) or a plain mapping (the MCP
            tool's arguments, where ``catalog`` may be a string or a list and
            ``last_nights``/``top_n`` may be ints). A key whose value is
            ``None`` counts as not given. Parameters other than the window,
            filters, and ``top_n`` are ignored here; rejecting them is the
            caller's.
        now: The aware query moment, read once by the caller.

    Returns:
        The frozen, validated query.

    Raises:
        InvalidQuery: Any invalid window, filter, or ``top_n`` value
            (``invalid_parameter`` naming the parameter or parameters).
        ApiError: ``filters_span_catalogs`` for more than one catalog.
        ValueError: If ``now`` is naive.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    now = now.astimezone(timezone.utc)
    return SummaryQuery(
        window=parse_window(params, now),
        filters=parse_filters(params),
        top_n=parse_top_n(params),
    )


# --- aggregation (KTD1, KTD5, KTD6, KTD10) ------------------------------------

SUMMARY_PATH = '/api/summary'

_CROSSMATCHED = (Alert.Status.MATCHED.value, Alert.Status.NOTIFIED.value)
_PENDING = (Alert.Status.INGESTED.value, Alert.Status.QUEUED.value)

# Per-catalog buckets, in reporting order (R9, R11; KTD6). Pending objects are
# not in any catalog's buckets; they are counted once for the window.
COINCIDENT = ObjectStatus.COINCIDENT_SOURCES.value
SEARCHED_NO_MATCH = SummaryCatalogOutcome.SEARCHED_NO_MATCH.value
CATALOG_BUCKETS: tuple[str, ...] = (
    COINCIDENT,
    SEARCHED_NO_MATCH,
    CatalogSearchOutcome.OUTSIDE_FOOTPRINT.value,
    CatalogSearchOutcome.SKIPPED_READ_FAILURE.value,
    CatalogSearchOutcome.NOT_SEARCHED_INVALID_POSITION.value,
    ReadTimeCatalogOutcome.NOT_IN_SERVICE_AT_CROSSMATCH.value,
    ReadTimeCatalogOutcome.NOT_RECORDED.value,
)

# Basis of the stored observation time (KTD9); NULL rows predate recording.
BASIS_NOT_RECORDED = 'not_recorded'

HISTOGRAM_BINS = 10

UNIT_DESCRIPTION = (
    'Every count is of distinct Rubin objects (diaObjectId), not detections or '
    'broker deliveries. The service keeps one row per object and only the first '
    'delivery from each broker, so these counts differ from delivery counts such '
    'as the ingest metric.'
)
CLOCK_DESCRIPTION = (
    "Objects fall into nights and windows by their stored observation time "
    "(event_time, UTC). A Rubin night is named by its dayObs, the UTC date of a "
    "time minus 12 hours, and spans dayObs 12:00Z to the next day's 12:00Z."
)
_EMPTY_WINDOW_NOTE = (
    'No objects in this window. Alerts may be paused (Rubin or a broker not '
    'sending); latest_object gives the observation time and night of the most '
    'recent object the service holds.'
)
_BASIS_NOTE = (
    'Some objects in this window were placed by a time other than their first '
    'detection (see basis): by a later detection, broker receive time, ingest '
    'time, or before the basis was recorded. Those objects may sit in a later '
    'night than their first detection. Earlier rows are not backfilled.'
)
_RELIABILITY_NOTE = (
    "Each object's reliability is the score captured once, from whichever "
    'broker delivered the object first (first_broker); for Lasair that is its '
    'latest-detection score. Objects with no stored score are counted in '
    'no_score and left out of the distribution and the top list.'
)


def _late_deliveries_note(provisional_days: int) -> str:
    return (
        'Any night, however old, can still gain objects when a broker first '
        'delivers an object detected earlier. Nights flagged provisional ended '
        f'less than {provisional_days} days ago, while most of that growth is '
        'still expected.'
    )


def _iso_z(moment: datetime | None) -> str | None:
    """An aware datetime as ISO-8601 UTC with a ``Z`` suffix, or ``None``."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _float(value: Any) -> float | None:
    """A database numeric as a finite ``float``, or ``None``."""
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _column(model: Any, field: str) -> str:
    return model._meta.get_field(field).column


def _window_sql(query: SummaryQuery) -> tuple[str, list[Any]]:
    """The ``WITH`` clause every statement shares (KTD5).

    ``base`` is the window with the broker filter applied; ``w`` adds the
    catalog filter. Pending objects belong to neither catalog-filter result
    (R14), so a catalog filter also restricts ``w`` to crossmatched objects.

    Returns:
        The SQL and its parameters.
    """
    alerts = Alert._meta.db_table
    oid = _column(Alert, 'lsst_diaObject_diaObjectId')
    deliveries = AlertDelivery._meta.db_table
    delivery_oid = _column(AlertDelivery, 'alert')
    matches = CatalogMatch._meta.db_table
    match_oid = _column(CatalogMatch, 'alert')

    params: list[Any] = [query.window.start, query.window.end]
    broker_sql = ''
    if query.filters.broker is not None:
        broker_sql = (
            f'AND EXISTS (SELECT 1 FROM {deliveries} d '
            f'WHERE d.{delivery_oid} = a.{oid} AND d.broker = %s)'
        )
        params.append(query.filters.broker)

    catalog_sql = 'TRUE'
    if query.filters.catalog is not None:
        negate = 'NOT ' if query.filters.catalog_match == CATALOG_MATCH_WITHOUT else ''
        catalog_sql = (
            f'b.status IN (%s, %s) AND {negate}EXISTS (SELECT 1 FROM {matches} m '
            f'WHERE m.{match_oid} = b.oid AND m.catalog_name = %s)'
        )
        params.extend([*_CROSSMATCHED, query.filters.catalog])

    sql = f'''
        WITH base AS (
            SELECT a.{oid} AS oid, a.status, a.reliability, a.event_time,
                   a.event_time_basis
            FROM {alerts} a
            WHERE a.event_time >= %s AND a.event_time < %s
            {broker_sql}
        ),
        w AS (SELECT * FROM base b WHERE {catalog_sql})
    '''
    return sql, params


def _fetch(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    """Run one statement under the request budget and return its rows."""
    check_deadline()
    with sql_phase():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def _current_record_sql() -> str:
    """A ``LATERAL`` join to ``w``'s current crossmatch record as ``rec``."""
    records = ObjectCrossmatchRecord._meta.db_table
    record_oid = _column(ObjectCrossmatchRecord, 'alert')
    return f'''
        LEFT JOIN LATERAL (
            SELECT r.catalog_outcomes FROM {records} r
            WHERE r.{record_oid} = w.oid
            ORDER BY r.match_version DESC LIMIT 1
        ) rec ON TRUE
    '''


def _night_rows(
    window_sql: str, params: list[Any]
) -> tuple[dict[date, int], dict[str, int]]:
    """Objects per dayObs and per observation-time basis (R6, R21)."""
    rows = _fetch(
        window_sql
        + '''
        SELECT ((w.event_time AT TIME ZONE 'UTC') - interval '12 hours')::date,
               w.event_time_basis, count(*)
        FROM w GROUP BY 1, 2
        ''',
        params,
    )
    per_night: dict[date, int] = {}
    basis = dict.fromkeys([*Alert.EventTimeBasis.values, BASIS_NOT_RECORDED], 0)
    for night, value, count in rows:
        per_night[night] = per_night.get(night, 0) + int(count)
        key = BASIS_NOT_RECORDED if value is None else str(value)
        basis[key] = basis.get(key, 0) + int(count)
    return per_night, basis


def _pending_count(window_sql: str, params: list[Any]) -> int:
    """Objects not yet crossmatched, before any catalog filter (R9, R14)."""
    rows = _fetch(
        window_sql + 'SELECT count(*) FROM base WHERE base.status IN (%s, %s)',
        [*params, *_PENDING],
    )
    return int(rows[0][0])


def _reliability(window_sql: str, params: list[Any]) -> dict[str, Any]:
    """Min, median, max, a 10-bin histogram on [0, 1], and the no-score count (R10)."""
    bucket = (
        f'LEAST(GREATEST(width_bucket(w.reliability, 0, 1, {HISTOGRAM_BINS}), 1), '
        f'{HISTOGRAM_BINS})'
    )
    bins = ', '.join(
        f'count(*) FILTER (WHERE w.reliability IS NOT NULL AND {bucket} = {i})'
        for i in range(1, HISTOGRAM_BINS + 1)
    )
    row = _fetch(
        window_sql
        + f'''
        SELECT min(w.reliability),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY w.reliability),
               max(w.reliability),
               count(*) FILTER (WHERE w.reliability IS NULL),
               {bins}
        FROM w
        ''',
        params,
    )[0]
    return {
        'min': _float(row[0]),
        'median': _float(row[1]),
        'max': _float(row[2]),
        'histogram': [
            {
                'low': round(i / HISTOGRAM_BINS, 1),
                'high': round((i + 1) / HISTOGRAM_BINS, 1),
                'objects': int(count),
            }
            for i, count in enumerate(row[4:])
        ],
        'no_score': int(row[3]),
    }


def _brokers(window_sql: str, params: list[Any]) -> dict[str, Any]:
    """Objects per delivering broker and the multi-broker overlap (R8)."""
    deliveries = AlertDelivery._meta.db_table
    delivery_oid = _column(AlertDelivery, 'alert')
    rows = _fetch(
        window_sql
        + f'''
        SELECT x.brokers, count(*) FROM (
            SELECT array_agg(DISTINCT d.broker ORDER BY d.broker) AS brokers
            FROM {deliveries} d JOIN w ON w.oid = d.{delivery_oid}
            GROUP BY d.{delivery_oid}
        ) x GROUP BY x.brokers
        ''',
        params,
    )
    counts = dict.fromkeys(broker_names(), 0)
    multi = 0
    for brokers, count in rows:
        for broker in brokers:
            if broker in counts:
                counts[broker] += int(count)
        if len(brokers) > 1:
            multi += int(count)
    return {'objects': counts, 'multi_broker': multi}


def _catalogs(
    window_sql: str, params: list[Any], query: SummaryQuery, pending: int
) -> dict[str, Any]:
    """Per-catalog outcome buckets of the crossmatched objects (R9, R11, R14; KTD6).

    Derived as ``api/lookup.py`` derives them: an object with a match row of
    the catalog is coincident whatever its record says (so pre-v0.15.0 objects
    with a match count too); otherwise its current record (highest
    ``match_version``) decides: no record is ``not_recorded``, a catalog the
    record does not name is ``not_in_service_at_crossmatch``, ``searched`` is
    ``searched_no_match``, and any other stored outcome is its own bucket.
    """
    names = catalog_names()
    matches = CatalogMatch._meta.db_table
    match_oid = _column(CatalogMatch, 'alert')
    records = ObjectCrossmatchRecord._meta.db_table
    record_oid = _column(ObjectCrossmatchRecord, 'alert')
    # Each object's current record and matched catalogs are read once, by hash
    # join over the window, then fanned out per catalog; a per-(object,
    # catalog) lookup was the dominant cost over long windows.
    rows = _fetch(
        window_sql
        + f'''
        , obj AS (
            SELECT w.oid, rec.catalog_outcomes, mc.cats
            FROM w
            LEFT JOIN (
                SELECT DISTINCT ON (r.{record_oid}) r.{record_oid} AS oid,
                       r.catalog_outcomes
                FROM {records} r JOIN w ON w.oid = r.{record_oid}
                ORDER BY r.{record_oid}, r.match_version DESC
            ) rec ON rec.oid = w.oid
            LEFT JOIN (
                SELECT m.{match_oid} AS oid, array_agg(DISTINCT m.catalog_name) AS cats
                FROM {matches} m JOIN w ON w.oid = m.{match_oid}
                GROUP BY m.{match_oid}
            ) mc ON mc.oid = w.oid
            WHERE w.status IN (%s, %s)
        )
        , combos AS (
            SELECT obj.catalog_outcomes, obj.cats, count(*) AS objects
            FROM obj GROUP BY 1, 2
        )
        SELECT c.name, CASE
            WHEN c.name = ANY(combos.cats) THEN %s
            WHEN combos.catalog_outcomes IS NULL THEN %s
            WHEN combos.catalog_outcomes -> c.name IS NULL THEN %s
            WHEN combos.catalog_outcomes ->> c.name = %s THEN %s
            ELSE combos.catalog_outcomes ->> c.name
        END, sum(combos.objects)
        FROM combos CROSS JOIN unnest(%s::text[]) AS c(name)
        GROUP BY 1, 2
        ''',
        [
            *params,
            *_CROSSMATCHED,
            COINCIDENT,
            ReadTimeCatalogOutcome.NOT_RECORDED.value,
            ReadTimeCatalogOutcome.NOT_IN_SERVICE_AT_CROSSMATCH.value,
            CatalogSearchOutcome.SEARCHED.value,
            SEARCHED_NO_MATCH,
            names,
        ],
    )
    by_catalog = {name: dict.fromkeys(CATALOG_BUCKETS, 0) for name in names}
    for name, bucket, count in rows:
        buckets = by_catalog[name]
        if bucket not in buckets:
            logger.warning(
                'Unknown stored catalog outcome; not counted in the summary',
                catalog=name,
                outcome=bucket,
                objects=int(count),
            )
            continue
        buckets[bucket] += int(count)
    if query.filters.catalog_match == CATALOG_MATCH_WITHOUT:
        # The reasons, never a single "no match" total (R14; KTD6).
        del by_catalog[query.filters.catalog][COINCIDENT]
    return {'by_catalog': by_catalog, 'crossmatch_pending': pending}


def _object_status(
    status: str, has_match: bool, has_record: bool, any_searched: bool
) -> str:
    """The object's status code, as ``api/lookup.py`` derives it (R8 of lookup)."""
    if status not in _CROSSMATCHED:
        return ObjectStatus.CROSSMATCH_PENDING.value
    if has_match:
        return ObjectStatus.COINCIDENT_SOURCES.value
    if has_record and not any_searched:
        return ObjectStatus.NOT_SEARCHED.value
    return ObjectStatus.NO_COINCIDENT_SOURCE.value


def _top(window_sql: str, params: list[Any], top_n: int) -> list[dict[str, Any]]:
    """The ``top_n`` objects by reliability, ties by diaObjectId (R10, R16)."""
    deliveries = AlertDelivery._meta.db_table
    delivery_oid = _column(AlertDelivery, 'alert')
    matches = CatalogMatch._meta.db_table
    match_oid = _column(CatalogMatch, 'alert')
    rows = _fetch(
        window_sql
        + f'''
        SELECT w.oid, w.reliability, w.event_time, w.status,
            (SELECT d.broker FROM {deliveries} d WHERE d.{delivery_oid} = w.oid
             ORDER BY d.ingest_time, d.id LIMIT 1),
            ARRAY(SELECT DISTINCT d.broker FROM {deliveries} d
                  WHERE d.{delivery_oid} = w.oid ORDER BY 1),
            EXISTS (SELECT 1 FROM {matches} m WHERE m.{match_oid} = w.oid),
            rec.catalog_outcomes IS NOT NULL,
            EXISTS (SELECT 1 FROM jsonb_each_text(rec.catalog_outcomes) o
                    WHERE o.value = %s)
        FROM (
            SELECT * FROM w WHERE w.reliability IS NOT NULL
            ORDER BY w.reliability DESC, w.oid ASC LIMIT %s
        ) w
        {_current_record_sql()}
        ORDER BY w.reliability DESC, w.oid ASC
        ''',
        [*params, CatalogSearchOutcome.SEARCHED.value, top_n],
    )
    top = []
    for (
        oid,
        reliability,
        observed,
        status,
        first,
        brokers,
        has_match,
        has_record,
        searched,
    ) in rows:
        top.append(
            {
                **dia_object_id_fields(oid),
                'reliability': _float(reliability),
                'first_broker': first,
                'brokers': [str(b) for b in brokers],
                'observed_at': _iso_z(observed),
                'night': day_obs(observed).isoformat(),
                'status': _object_status(
                    str(status), bool(has_match), bool(has_record), bool(searched)
                ),
            }
        )
    return top


def _latest_object() -> dict[str, str] | None:
    """The most recent stored observation, ignoring every filter (R18)."""
    check_deadline()
    with sql_phase():
        latest = Alert.objects.aggregate(latest=Max('event_time'))['latest']
    if latest is None:
        return None
    return {'observed_at': _iso_z(latest), 'night': day_obs(latest).isoformat()}


def _request_url(query: SummaryQuery) -> str:
    """The relative REST request reproducing this answer (R19).

    Built only from parsed values. Windows that depend on the clock (the
    default night and ``last_nights``) are pinned to the nights they resolved
    to, so the request gives the same window whenever it is replayed.
    """
    window = query.window
    pairs: list[tuple[str, str]] = []
    if window.form in (FORM_DEFAULT, FORM_NIGHT):
        pairs.append((NIGHT_PARAM, window.nights[0].isoformat()))
    else:
        pairs.append((START_PARAM, _iso_z(window.start)))
        pairs.append((END_PARAM, _iso_z(window.end)))
    if query.filters.broker is not None:
        pairs.append((BROKER_PARAM, query.filters.broker))
    if query.filters.catalog is not None:
        pairs.append((CATALOG_PARAM, query.filters.catalog))
        pairs.append((CATALOG_MATCH_PARAM, query.filters.catalog_match))
    pairs.append((TOP_N_PARAM, str(query.top_n)))
    return f'{SUMMARY_PATH}?{urlencode(pairs)}'


def summarize_window(
    params: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """The window summary: every statistic for one window and its filters (R1).

    Runs a handful of statements over one filtered window set, each under
    ``sql_phase``. It opens no transaction: callers wrap it in the guarded
    REPEATABLE READ transaction (KTD4) so every number comes from one
    snapshot.

    Args:
        params: The raw request parameters (see ``parse_summary_params``).
        now: The aware query moment; defaults to the current time. Echoed as
            ``as_of`` and used for the window and provisional flags.

    Returns:
        The enveloped, JSON-native body: ``unit``, ``clock``, ``window``,
        ``filters``, ``as_of``, ``request``, ``notes``, then ``totals``,
        ``brokers``, ``catalogs``, ``reliability``, ``top``, ``basis``, and,
        for an empty window, ``latest_object``.

    Raises:
        InvalidQuery: Any invalid parameter (``invalid_parameter``).
        ApiError: ``filters_span_catalogs`` for more than one catalog.
        DeadlineExceeded: If the request runs over its budget.
    """
    now = (now if now is not None else django_timezone.now()).astimezone(timezone.utc)
    query = parse_summary_params(params, now=now)
    window = query.window
    window_sql, sql_params = _window_sql(query)

    per_night, basis = _night_rows(window_sql, sql_params)
    pending = _pending_count(window_sql, sql_params)
    reliability = _reliability(window_sql, sql_params)
    brokers = _brokers(window_sql, sql_params)
    catalogs = _catalogs(window_sql, sql_params, query, pending)
    top = _top(window_sql, sql_params, query.top_n)

    total = sum(per_night.values())
    provisional_days = int(settings.SUMMARY_PROVISIONAL_DAYS)
    provisional_after = now - timedelta(days=provisional_days)
    nights = [
        {
            'night': night.isoformat(),
            'objects': per_night.get(night, 0),
            'provisional': night_end(night) > provisional_after,
            'partial': window.is_partial(night),
        }
        for night in window.nights
    ]

    notes: dict[str, str] = {'late_deliveries': _late_deliveries_note(provisional_days)}
    if total - reliability['no_score'] > 0:
        notes['reliability_capture'] = _RELIABILITY_NOTE
    if total - basis[Alert.EventTimeBasis.FIRST_DETECTION.value] > 0:
        notes['basis'] = _BASIS_NOTE
    if total == 0:
        notes['empty_window'] = _EMPTY_WINDOW_NOTE

    body: dict[str, Any] = {
        'unit': {'counts': 'distinct_rubin_objects', 'description': UNIT_DESCRIPTION},
        'clock': {
            'field': 'event_time',
            'timezone': 'UTC',
            'description': CLOCK_DESCRIPTION,
        },
        'window': {
            'form': window.form,
            'nights': [night.isoformat() for night in window.nights],
            'start': _iso_z(window.start),
            'end': _iso_z(window.end),
            'first_night_partial': window.first_partial,
            'last_night_partial': window.last_partial,
        },
        'filters': {
            'broker': query.filters.broker,
            'catalog': query.filters.catalog,
            'catalog_match': query.filters.catalog_match,
        },
        'as_of': _iso_z(now),
        'request': _request_url(query),
        'notes': notes,
        'totals': {'objects': total, 'nights': nights},
        'brokers': brokers,
        'catalogs': catalogs,
        'reliability': reliability,
        'top': top,
        'basis': basis,
    }
    if total == 0:
        body['latest_object'] = _latest_object()
    return envelope(body)
