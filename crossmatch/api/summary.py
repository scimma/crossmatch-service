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

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from django.conf import settings

from api.contract import ErrorCode
from api.errors import ApiError, InvalidQuery
from api.filters import CATALOG_FILTER
from api.timeparse import parse_timestamp
from core.provenance import reliability_cuts

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


def _max_nights() -> int:
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
    count = _parse_int(raw, LAST_NIGHTS_PARAM, _max_nights())
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
    start = parse_timestamp(raw_start, START_PARAM).astimezone(timezone.utc)
    end = parse_timestamp(raw_end, END_PARAM).astimezone(timezone.utc)
    if end <= start:
        raise InvalidQuery('end must be after start', params=span_params)
    first = day_obs(start)
    # Half-open: the last instant inside the span is just before ``end``.
    last = day_obs(end - timedelta(microseconds=1))
    touched = (last - first).days + 1
    maximum = _max_nights()
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

    configured = [cat['name'] for cat in settings.CROSSMATCH_CATALOGS]
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
