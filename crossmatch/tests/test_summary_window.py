"""U3: window parsing and night arithmetic of the window summary (KTD2, KTD3).

Pure parsing with a frozen ``now``; no database. Covers AE6 (31-night limit)
and AE9 (no window before the evening's observing means the previous night),
plus the broker, catalog, and ``top_n`` parameters (KTD7, KTD8).
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from django.http import QueryDict
from django.test import override_settings

from api.contract import ErrorCode
from api.errors import ApiError, InvalidQuery
from api.summary import parse_summary_params

UTC = timezone.utc


def _at(text: str) -> datetime:
    """A frozen clock reading, ISO-8601 UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _parse(query: str, now: str = '2026-11-03T15:00'):
    return parse_summary_params(QueryDict(query), now=_at(now))


def _error(query: str, now: str = '2026-11-03T15:00') -> ApiError:
    with pytest.raises(ApiError) as excinfo:
        _parse(query, now)
    return excinfo.value


# --- default night (KTD2, AE9) ---------------------------------------------


@pytest.mark.parametrize(
    'now,expected',
    [
        ('2026-11-03T15:00', date(2026, 11, 2)),
        ('2026-11-03T23:30', date(2026, 11, 3)),
        ('2026-11-04T03:00', date(2026, 11, 3)),
    ],
)
def test_default_night_is_latest_begun_night(now, expected):
    window = _parse('', now).window
    assert window.form == 'default'
    assert window.nights == (expected,)
    assert window.start == datetime(
        expected.year, expected.month, expected.day, 12, tzinfo=UTC
    )
    assert window.end == window.start + timedelta(days=1)
    assert not window.first_partial and not window.last_partial


def test_rollover_hours_read_at_call_time():
    with override_settings(SUMMARY_NIGHT_ROLLOVER_HOURS=12):
        assert _parse('', '2026-11-03T15:00').window.nights == (date(2026, 11, 3),)


# --- night ------------------------------------------------------------------


def test_night_resolves_to_noon_to_noon_utc():
    window = _parse('night=2026-11-02').window
    assert window.form == 'night'
    assert window.nights == (date(2026, 11, 2),)
    assert window.start == _at('2026-11-02T12:00')
    assert window.end == _at('2026-11-03T12:00')


def test_night_not_begun_is_rejected_then_accepted_once_begun():
    exc = _error('night=2026-11-03', '2026-11-03T15:00')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'night'
    assert _parse('night=2026-11-03', '2026-11-03T23:30').window.nights == (
        date(2026, 11, 3),
    )


@pytest.mark.parametrize('value', ['2026-11-31', '20261102', 'last night', ''])
def test_malformed_night_is_rejected(value):
    exc = _error(f'night={value}')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'night'


# --- last_nights ------------------------------------------------------------


def test_last_nights_ends_at_default_night():
    window = _parse('last_nights=7').window
    assert window.form == 'last_nights'
    assert window.nights == tuple(
        date(2026, 10, 27) + timedelta(days=i) for i in range(7)
    )
    assert window.start == _at('2026-10-27T12:00')
    assert window.end == _at('2026-11-03T12:00')


@pytest.mark.parametrize('value', ['0', '32', '-1', '1.5', 'seven'])
def test_last_nights_out_of_range_or_non_integer(value):
    exc = _error(f'last_nights={value}')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'last_nights'
    assert '31' in exc.message


def test_last_nights_accepts_int_from_plain_mapping():
    query = parse_summary_params({'last_nights': 3}, now=_at('2026-11-03T15:00'))
    assert len(query.window.nights) == 3


# --- start + end span -------------------------------------------------------


def test_span_counts_every_touched_night_and_flags_cut_edges():
    window = _parse('start=2026-11-01T00:00Z&end=2026-11-03T00:00Z').window
    assert window.form == 'span'
    assert window.nights == (date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2))
    assert window.start == _at('2026-11-01T00:00')
    assert window.end == _at('2026-11-03T00:00')
    assert window.first_partial and window.last_partial


def test_span_on_night_boundaries_is_not_partial():
    window = _parse('start=2026-11-01T12:00Z&end=2026-11-03T12:00Z').window
    assert window.nights == (date(2026, 11, 1), date(2026, 11, 2))
    assert not window.first_partial and not window.last_partial


def test_naive_timestamp_is_utc():
    window = _parse('start=2026-11-01T12:00&end=2026-11-02T12:00').window
    assert window.start == _at('2026-11-01T12:00')
    assert window.nights == (date(2026, 11, 1),)


def test_offset_timestamp_converts_to_utc():
    window = _parse('start=2026-11-01T09:00-03:00&end=2026-11-02T12:00Z').window
    assert window.start == _at('2026-11-01T12:00')
    assert window.start.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    'query',
    [
        # 60 days (AE6).
        'start=2026-09-03T12:00Z&end=2026-11-02T12:00Z',
        # 32 touched nights (10-02 .. 11-02): 30 whole nights plus two cut edges.
        'start=2026-10-02T13:00Z&end=2026-11-02T13:00Z',
    ],
)
def test_span_over_limit_names_31(query):
    exc = _error(query)
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.params == ['start', 'end']
    assert '31' in exc.message


def test_span_of_exactly_31_nights_is_accepted():
    window = _parse('start=2026-10-02T13:00Z&end=2026-11-02T00:00Z').window
    assert len(window.nights) == 31


@pytest.mark.parametrize(
    'query',
    [
        'start=2026-11-02T00:00Z&end=2026-11-01T00:00Z',
        'start=2026-11-02T00:00Z&end=2026-11-02T00:00Z',
    ],
)
def test_end_not_after_start_is_rejected(query):
    exc = _error(query)
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.params == ['start', 'end']


@pytest.mark.parametrize(
    'query,param',
    [
        ('start=yesterday&end=2026-11-02T00:00Z', 'start'),
        ('start=2026-11-01T00:00Z&end=2026-13-01T00:00', 'end'),
    ],
)
def test_malformed_timestamp_names_parameter(query, param):
    exc = _error(query)
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == param


def test_start_without_end_is_rejected():
    exc = _error('start=2026-11-01T00:00Z')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.params == ['start', 'end']


def test_span_ending_after_latest_begun_night_is_rejected():
    # At 15:00Z on 2026-11-03 the latest begun night (11-02) ends at 11-03 12:00Z.
    ok = _parse('start=2026-11-02T00:00Z&end=2026-11-03T12:00Z').window
    assert ok.nights[-1] == date(2026, 11, 2)
    exc = _error('start=2026-11-02T00:00Z&end=2026-11-03T15:00Z')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'end'


@pytest.mark.parametrize(
    'query,params',
    [
        (
            'night=2026-11-02&start=2026-11-01T00:00Z&end=2026-11-02T00:00Z',
            ['night', 'start', 'end'],
        ),
        ('night=2026-11-02&last_nights=3', ['night', 'last_nights']),
    ],
)
def test_window_forms_are_mutually_exclusive(query, params):
    exc = _error(query)
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.params == params


# --- broker (KTD8) ----------------------------------------------------------


@pytest.mark.parametrize(
    'value,expected',
    [
        ('Pitt-Google', 'pittgoogle'),
        ('ANTARES', 'antares'),
        ('pitt google', 'pittgoogle'),
        ('lasair', 'lasair'),
    ],
)
def test_broker_spellings_normalize(value, expected):
    query = parse_summary_params({'broker': value}, now=_at('2026-11-03T15:00'))
    assert query.filters.broker == expected


def test_unknown_broker_lists_valid_values():
    exc = _error('broker=ztf')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'broker'
    for name in ('antares', 'lasair', 'pittgoogle'):
        assert name in exc.message


def test_no_filters_by_default():
    filters = _parse('').filters
    assert filters.broker is None
    assert filters.catalog is None
    assert filters.catalog_match is None


# --- catalog + catalog_match (KTD7) -----------------------------------------


def test_catalog_defaults_to_with_and_accepts_without():
    assert _parse('catalog=delve_dr3_gold').filters.catalog_match == 'with'
    filters = _parse('catalog=delve_dr3_gold&catalog_match=without').filters
    assert filters.catalog == 'delve_dr3_gold'
    assert filters.catalog_match == 'without'


def test_two_catalogs_span_catalogs():
    exc = _error('catalog=gaia_dr3&catalog=des_y6_gold')
    assert exc.code == ErrorCode.FILTERS_SPAN_CATALOGS
    assert not isinstance(exc, InvalidQuery)
    assert exc.params == ['catalog', 'catalog']
    assert 'gaia_dr3' in exc.message and 'des_y6_gold' in exc.message


def test_two_catalogs_from_plain_mapping_list():
    with pytest.raises(ApiError) as excinfo:
        parse_summary_params(
            {'catalog': ['gaia_dr3', 'skymapper_dr4']}, now=_at('2026-11-03T15:00')
        )
    assert excinfo.value.code == ErrorCode.FILTERS_SPAN_CATALOGS


def test_catalog_match_requires_catalog():
    exc = _error('catalog_match=without')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'catalog_match'


def test_catalog_match_value_is_checked():
    exc = _error('catalog=gaia_dr3&catalog_match=maybe')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'catalog_match'


def test_unknown_catalog_lists_configured_names():
    exc = _error('catalog=gaia')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'catalog'
    assert 'gaia_dr3' in exc.message and 'des_y6_gold' in exc.message


# --- top_n --------------------------------------------------------------------


def test_top_n_defaults_and_accepts_cap():
    assert _parse('').top_n == 10
    assert _parse('top_n=20').top_n == 20


@pytest.mark.parametrize('value', ['21', '0', 'ten', '2.5'])
def test_top_n_out_of_range_or_non_integer(value):
    exc = _error(f'top_n={value}')
    assert exc.code == ErrorCode.INVALID_PARAMETER
    assert exc.param == 'top_n'
    assert '20' in exc.message


def test_top_n_cap_read_at_call_time():
    with override_settings(MCP_MAX_OBJECTS=5):
        assert _error('top_n=6').param == 'top_n'


def test_query_is_frozen():
    query = _parse('')
    with pytest.raises(AttributeError):
        query.top_n = 3
