"""U4: the window summary aggregation service (KTD1, KTD5, KTD6, KTD10).

Covers R1, R3, R4, R6-R11, R13, R14, R16, R18, R19, R21 and AE1-AE5, AE7, AE8
against ``api.summary.summarize_window`` with a frozen ``now``. Parsing is
covered by ``test_summary_window``; the REST and MCP adapters by their own
units.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from django.http import QueryDict

from api.summary import night_end, night_start, summarize_window
from core.models import Alert, AlertDelivery
from tests.factories import (
    AlertDeliveryFactory,
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

UTC = timezone.utc
MATCHED = Alert.Status.MATCHED
NOTIFIED = Alert.Status.NOTIFIED
INGESTED = Alert.Status.INGESTED
QUEUED = Alert.Status.QUEUED
FIRST = Alert.EventTimeBasis.FIRST_DETECTION

# The clock: 15:00Z on 2026-11-10, so the latest begun night is 2026-11-09.
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
NIGHT = date(2026, 11, 9)
CATALOGS = ['gaia_dr3', 'des_y6_gold', 'delve_dr3_gold', 'skymapper_dr4']
BUCKETS = [
    'coincident_sources',
    'searched_no_match',
    'outside_footprint',
    'skipped_read_failure',
    'not_searched_invalid_position',
    'not_in_service_at_crossmatch',
    'not_recorded',
]

_next_id = iter(range(5_000_000_000, 6_000_000_000))


def _summary(query: str = '', now: datetime = NOW) -> dict:
    return summarize_window(QueryDict(query), now=now)


def _in_night(night: date, hours: float = 6) -> datetime:
    """A moment inside Rubin night ``night``."""
    return night_start(night) + timedelta(hours=hours)


def _deliver(alert: Alert, broker: str, when: datetime | None = None) -> None:
    """Record a broker delivery of ``alert`` at ``when`` (ingest_time pinned)."""
    delivery = AlertDeliveryFactory(alert=alert, broker=broker)
    if when is not None:
        AlertDelivery.objects.filter(pk=delivery.pk).update(ingest_time=when)


def _obj(
    night: date = NIGHT,
    *,
    oid: int | None = None,
    status: str = MATCHED,
    reliability: float | None = 0.5,
    basis: str | None = FIRST,
    brokers: tuple[str, ...] = ('antares',),
    outcomes: dict[str, str] | None | bool = True,
    matches: tuple[str, ...] = (),
    hours: float = 6,
) -> Alert:
    """One Rubin object observed in ``night``, with deliveries, record, matches.

    ``outcomes=True`` writes a record with every catalog searched, a dict writes
    that record, and ``None``/``False`` writes none (pre-v0.15.0). Brokers are
    delivered in order, an hour apart, starting after the observation.
    """
    observed = _in_night(night, hours)
    alert = AlertFactory(
        lsst_diaObject_diaObjectId=oid if oid is not None else next(_next_id),
        event_time=observed,
        event_time_basis=basis,
        status=status,
        reliability=reliability,
    )
    for i, broker in enumerate(brokers):
        _deliver(alert, broker, observed + timedelta(hours=1 + i))
    if status in (MATCHED, NOTIFIED) and outcomes:
        kwargs = {} if outcomes is True else {'catalog_outcomes': outcomes}
        ObjectCrossmatchRecordFactory(alert=alert, **kwargs)
    for catalog in matches:
        CatalogMatchFactory(alert=alert, catalog_name=catalog)
    return alert


def _searched(**overrides: str) -> dict[str, str]:
    outcomes = {name: 'searched' for name in CATALOGS}
    outcomes.update(overrides)
    return outcomes


def _night_row(body: dict, night: date) -> dict:
    return next(
        row for row in body['totals']['nights'] if row['night'] == night.isoformat()
    )


# --- body shape (R4, R19; KTD1) --------------------------------------------


@pytest.mark.django_db
def test_body_leads_with_answer_metadata_and_serializes_strictly():
    _obj(reliability=0.9, matches=('gaia_dr3',))

    body = _summary('night=2026-11-09&broker=Pitt-Google&top_n=5')

    keys = list(body)
    assert keys[:9] == [
        'provenance',
        'unit',
        'clock',
        'window',
        'filters',
        'as_of',
        'request',
        'notes',
        'totals',
    ]
    assert 'distinct' in body['unit']['description'].lower()
    assert 'deliver' in body['unit']['description'].lower()
    assert body['clock']['field'] == 'event_time'
    assert body['clock']['timezone'] == 'UTC'
    assert body['window'] == {
        'form': 'night',
        'nights': ['2026-11-09'],
        'start': '2026-11-09T12:00:00Z',
        'end': '2026-11-10T12:00:00Z',
        'first_night_partial': False,
        'last_night_partial': False,
    }
    assert body['filters'] == {
        'broker': 'pittgoogle',
        'catalog': None,
        'catalog_match': None,
    }
    assert body['as_of'] == '2026-11-10T15:00:00Z'
    assert body['request'] == '/api/summary?night=2026-11-09&broker=pittgoogle&top_n=5'
    json.dumps(body, allow_nan=False)


@pytest.mark.django_db
def test_request_pins_default_and_last_nights_windows_and_echoes_partial_span():
    default = _summary()
    assert default['window']['form'] == 'default'
    assert default['request'] == '/api/summary?night=2026-11-09&top_n=10'

    week = _summary('last_nights=7&catalog=gaia_dr3&catalog_match=without')
    assert week['window']['nights'][0] == '2026-11-03'
    assert week['request'] == (
        '/api/summary?start=2026-11-03T12%3A00%3A00Z&end=2026-11-10T12%3A00%3A00Z'
        '&catalog=gaia_dr3&catalog_match=without&top_n=10'
    )

    span = _summary('start=2026-11-08T18:00&end=2026-11-09T20:00')
    assert span['window']['nights'] == ['2026-11-08', '2026-11-09']
    assert span['window']['first_night_partial'] is True
    assert span['window']['last_night_partial'] is True
    assert [row['partial'] for row in span['totals']['nights']] == [True, True]


@pytest.mark.django_db
def test_late_delivery_note_is_always_present():
    assert 'gain objects' in _summary()['notes']['late_deliveries']
    _obj()
    assert 'gain objects' in _summary()['notes']['late_deliveries']


# --- counting by observation night and broker (R3, R6, R8; AE2) ------------


@pytest.mark.django_db
def test_object_counts_once_per_broker_and_in_overlap_for_its_observed_night_only():
    alert = _obj(brokers=('antares',))
    _deliver(alert, 'lasair', _in_night(NIGHT) + timedelta(days=7))

    body = _summary('night=2026-11-09', now=NOW + timedelta(days=8))

    assert body['totals']['objects'] == 1
    assert body['brokers'] == {
        'objects': {'antares': 1, 'lasair': 1, 'pittgoogle': 0},
        'multi_broker': 1,
    }
    assert body['top'][0]['first_broker'] == 'antares'
    assert body['top'][0]['brokers'] == ['antares', 'lasair']

    later = _summary('night=2026-11-16', now=NOW + timedelta(days=8))
    assert later['totals']['objects'] == 0
    assert later['brokers']['objects']['lasair'] == 0


@pytest.mark.django_db
def test_broker_filter_restricts_every_statistic_and_score_comes_from_first_broker():
    """AE3: a Lasair-delivered, Pitt-Google-first object ranks on its stored score."""
    first = _obj(brokers=('pittgoogle', 'lasair'), reliability=0.97)
    _obj(brokers=('lasair',), reliability=0.5)
    _obj(brokers=('pittgoogle',), reliability=0.99)

    body = _summary('night=2026-11-09&broker=lasair')

    assert body['totals']['objects'] == 2
    assert body['brokers']['objects'] == {'antares': 0, 'lasair': 2, 'pittgoogle': 1}
    assert body['brokers']['multi_broker'] == 1
    assert body['reliability']['max'] == 0.97
    top = body['top'][0]
    assert top['diaObjectId'] == first.lsst_diaObject_diaObjectId
    assert top['reliability'] == 0.97
    assert top['first_broker'] == 'pittgoogle'
    note = body['notes']['reliability_capture']
    assert 'first' in note and 'Lasair' in note and 'latest-detection' in note


# --- catalog outcomes (R9, R11, R13, R14; AE4) -----------------------------


@pytest.mark.django_db
def test_catalog_buckets_follow_lookup_status_derivation():
    _obj(matches=('gaia_dr3',))  # recorded, Gaia coincident
    _obj(outcomes=None, matches=('gaia_dr3',))  # pre-v0.15.0 with a Gaia match
    _obj(outcomes=None)  # pre-v0.15.0 without any match
    # Record predates SkyMapper; DES outside its footprint.
    predates = _searched(des_y6_gold='outside_footprint')
    del predates['skymapper_dr4']
    _obj(outcomes=predates)
    _obj(
        outcomes=_searched(
            delve_dr3_gold='skipped_read_failure',
            des_y6_gold='not_searched_invalid_position',
        )
    )
    _obj(status=INGESTED)
    _obj(status=QUEUED)

    body = _summary('night=2026-11-09')

    assert body['totals']['objects'] == 7
    assert body['catalogs']['crossmatch_pending'] == 2
    gaia = body['catalogs']['by_catalog']['gaia_dr3']
    assert gaia == dict.fromkeys(BUCKETS, 0) | {
        'coincident_sources': 2,
        'searched_no_match': 2,
        'not_recorded': 1,
    }
    assert body['catalogs']['by_catalog']['des_y6_gold'] == dict.fromkeys(
        BUCKETS, 0
    ) | {
        'searched_no_match': 1,
        'outside_footprint': 1,
        'not_searched_invalid_position': 1,
        'not_recorded': 2,
    }
    assert body['catalogs']['by_catalog']['delve_dr3_gold'] == dict.fromkeys(
        BUCKETS, 0
    ) | {
        'searched_no_match': 2,
        'skipped_read_failure': 1,
        'not_recorded': 2,
    }
    assert body['catalogs']['by_catalog']['skymapper_dr4'] == dict.fromkeys(
        BUCKETS, 0
    ) | {
        'searched_no_match': 2,
        'not_in_service_at_crossmatch': 1,
        'not_recorded': 2,
    }
    # Sum invariant: every catalog's buckets plus pending is the window total.
    for name in CATALOGS:
        buckets = body['catalogs']['by_catalog'][name]
        assert sum(buckets.values()) + body['catalogs']['crossmatch_pending'] == 7


@pytest.mark.django_db
def test_without_filter_splits_no_match_reasons_with_no_total_key():
    """AE4: searched-none, outside-footprint and not-recorded stay separate."""
    _obj(outcomes=_searched())
    _obj(outcomes=_searched(delve_dr3_gold='outside_footprint'))
    _obj(outcomes=None, matches=('gaia_dr3',))
    _obj(matches=('delve_dr3_gold',))
    _obj(status=INGESTED)

    body = _summary('night=2026-11-09&catalog=delve_dr3_gold&catalog_match=without')

    assert body['filters'] == {
        'broker': None,
        'catalog': 'delve_dr3_gold',
        'catalog_match': 'without',
    }
    assert body['totals']['objects'] == 3
    delve = body['catalogs']['by_catalog']['delve_dr3_gold']
    assert delve == {
        'searched_no_match': 1,
        'outside_footprint': 1,
        'skipped_read_failure': 0,
        'not_searched_invalid_position': 0,
        'not_in_service_at_crossmatch': 0,
        'not_recorded': 1,
    }
    assert not {'total', 'objects', 'coincident_sources'} & set(delve)
    # Pending objects are in neither result; the window count is context.
    assert body['catalogs']['crossmatch_pending'] == 1
    assert body['catalogs']['by_catalog']['gaia_dr3']['coincident_sources'] == 1


@pytest.mark.django_db
def test_with_filter_restricts_every_statistic_to_coincident_objects():
    hit = _obj(matches=('gaia_dr3',), reliability=0.8, brokers=('lasair',))
    _obj(reliability=0.9)
    _obj(status=QUEUED, reliability=0.95)

    body = _summary('night=2026-11-09&catalog=gaia_dr3')

    assert body['filters']['catalog_match'] == 'with'
    assert body['totals']['objects'] == 1
    assert body['brokers']['objects'] == {'antares': 0, 'lasair': 1, 'pittgoogle': 0}
    assert body['catalogs']['by_catalog']['gaia_dr3']['coincident_sources'] == 1
    assert body['reliability']['max'] == 0.8
    assert [t['diaObjectId'] for t in body['top']] == [hit.lsst_diaObject_diaObjectId]
    assert body['catalogs']['crossmatch_pending'] == 1


# --- reliability and top N (R10, R16) --------------------------------------


@pytest.mark.django_db
def test_null_reliability_is_counted_apart_and_ties_rank_by_id():
    _obj(oid=101, reliability=0.5)
    _obj(oid=100, reliability=0.5)
    _obj(oid=102, reliability=0.0)
    _obj(oid=103, reliability=1.0)
    _obj(oid=104, reliability=0.25)
    _obj(oid=105, reliability=None)
    _obj(oid=106, reliability=None)

    body = _summary('night=2026-11-09')

    rel = body['reliability']
    assert rel['no_score'] == 2
    assert (rel['min'], rel['median'], rel['max']) == (0.0, 0.5, 1.0)
    assert rel['histogram'] == [
        {'low': round(i / 10, 1), 'high': round((i + 1) / 10, 1), 'objects': n}
        for i, n in enumerate([1, 0, 1, 0, 0, 2, 0, 0, 0, 1])
    ]
    assert [t['diaObjectId'] for t in body['top']] == [103, 100, 101, 104, 102]


@pytest.mark.django_db
def test_top_n_defaults_honors_top_n_and_keeps_large_ids_exact():
    big = 2**53 + 1
    _obj(oid=big, reliability=0.99, matches=('gaia_dr3',))
    for i in range(11):
        _obj(reliability=0.1 + i / 100)

    body = _summary('night=2026-11-09')
    assert len(body['top']) == 10
    top = body['top'][0]
    assert top['diaObjectId'] == big
    assert top['diaObjectId_str'] == '9007199254740993'
    assert top['observed_at'] == '2026-11-09T18:00:00Z'
    assert top['night'] == '2026-11-09'
    assert top['status'] == 'coincident_sources'
    assert body['top'][1]['status'] == 'no_coincident_source'

    assert len(_summary('night=2026-11-09&top_n=3')['top']) == 3


@pytest.mark.django_db
def test_top_entries_carry_lookup_status_codes():
    pending = _obj(status=INGESTED, reliability=0.9)
    unsearched = _obj(
        reliability=0.8, outcomes=dict.fromkeys(CATALOGS, 'outside_footprint')
    )
    statuses = {
        t['diaObjectId']: t['status'] for t in _summary('night=2026-11-09')['top']
    }
    assert statuses[pending.lsst_diaObject_diaObjectId] == 'crossmatch_pending'
    assert statuses[unsearched.lsst_diaObject_diaObjectId] == 'not_searched'


# --- nights: provisional and late growth (R6, R7; AE5, AE8) ----------------


@pytest.mark.django_db
def test_provisional_flags_recent_nights_only():
    """AE5: two hours after a night ends it is provisional; eight days, not."""
    now = night_end(NIGHT) + timedelta(hours=2)
    body = _summary('last_nights=10', now=now)
    rows = {row['night']: row for row in body['totals']['nights']}
    assert rows['2026-11-09']['provisional'] is True
    assert rows['2026-11-01']['provisional'] is False
    assert rows['2026-11-04']['provisional'] is True
    assert rows['2026-11-03']['provisional'] is False


@pytest.mark.django_db
def test_late_first_delivery_raises_an_old_nights_count():
    """AE8: a Lasair-first object detected three weeks earlier joins that night."""
    old = NIGHT - timedelta(days=21)
    query = f'night={old.isoformat()}'
    _obj(old)
    assert _summary(query)['totals']['objects'] == 1

    alert = _obj(old, brokers=())
    _deliver(alert, 'lasair', NOW - timedelta(hours=1))

    body = _summary(query)
    assert body['totals']['objects'] == 2
    assert _night_row(body, old)['objects'] == 2
    assert _night_row(body, old)['provisional'] is False
    assert 'gain objects' in body['notes']['late_deliveries']


@pytest.mark.django_db
def test_per_night_totals_sum_to_the_window_total():
    for offset, count in [(0, 2), (1, 0), (2, 3), (6, 1)]:
        for _ in range(count):
            _obj(NIGHT - timedelta(days=offset))
    _obj(NIGHT - timedelta(days=7))  # outside a 7-night window

    body = _summary('last_nights=7')

    nights = body['totals']['nights']
    assert len(nights) == 7
    assert [row['objects'] for row in nights] == [1, 0, 0, 0, 3, 0, 2]
    assert sum(row['objects'] for row in nights) == body['totals']['objects'] == 6


# --- observation-time basis (R21; AE7) -------------------------------------


@pytest.mark.django_db
def test_basis_counts_include_before_recording():
    _obj(basis=None)
    _obj(basis=None)
    _obj(basis=FIRST)
    _obj(basis=Alert.EventTimeBasis.LATER_DETECTION)
    _obj(basis=Alert.EventTimeBasis.BROKER_RECEIVE)

    body = _summary('night=2026-11-09')

    assert body['basis'] == {
        'first_detection': 1,
        'later_detection': 1,
        'broker_receive': 1,
        'ingest_time': 0,
        'not_recorded': 2,
    }
    assert 'later night' in body['notes']['basis']


@pytest.mark.django_db
def test_basis_note_absent_when_every_object_is_first_detection():
    _obj(basis=FIRST)
    assert 'basis' not in _summary('night=2026-11-09')['notes']


# --- empty window (R18; AE1) -----------------------------------------------


@pytest.mark.django_db
def test_empty_window_reports_zeros_nulls_and_the_latest_stored_object():
    latest = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)
    AlertFactory(event_time=latest - timedelta(days=3))
    AlertFactory(event_time=latest, reliability=None)

    body = _summary()

    assert body['window']['nights'] == ['2026-11-09']
    assert body['totals'] == {
        'objects': 0,
        'nights': [
            {'night': '2026-11-09', 'objects': 0, 'provisional': True, 'partial': False}
        ],
    }
    assert body['brokers'] == {
        'objects': {'antares': 0, 'lasair': 0, 'pittgoogle': 0},
        'multi_broker': 0,
    }
    assert body['reliability']['min'] is None
    assert body['reliability']['median'] is None
    assert body['reliability']['max'] is None
    assert body['reliability']['no_score'] == 0
    assert all(b['objects'] == 0 for b in body['reliability']['histogram'])
    assert body['top'] == []
    assert body['catalogs']['crossmatch_pending'] == 0
    for name in CATALOGS:
        assert body['catalogs']['by_catalog'][name] == dict.fromkeys(BUCKETS, 0)
    assert body['latest_object'] == {
        'observed_at': '2026-09-14T04:30:00Z',
        'night': '2026-09-13',
    }
    assert 'paused' in body['notes']['empty_window']
    json.dumps(body, allow_nan=False)


@pytest.mark.django_db
def test_empty_window_with_no_objects_at_all_has_null_latest_object():
    body = _summary()
    assert body['latest_object'] is None


@pytest.mark.django_db
def test_latest_object_ignores_filters_and_is_absent_when_window_has_objects():
    _obj(brokers=('antares',))
    filtered = _summary('night=2026-11-09&broker=lasair')
    assert filtered['totals']['objects'] == 0
    assert filtered['latest_object']['night'] == '2026-11-09'

    assert 'latest_object' not in _summary('night=2026-11-09')
    assert 'empty_window' not in _summary('night=2026-11-09')['notes']
