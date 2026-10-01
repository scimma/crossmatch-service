"""U4 / R1, R2, R4, R9: the recent-crossmatch HTTP view.

Parameter parsing, defaults, 400s on bad input, the clamp-not-reject behavior
for an oversized page_size, cursor round-tripping through two GETs, cursor/param
conflict -> 400, and unauthenticated access on the DEV config. Query correctness
is covered by test_recent_crossmatch_service; these tests exercise the HTTP
adapter end-to-end through the URLconf.
"""

import json
import re
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

import pytest
from django.conf import settings
from django.db import OperationalError, connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from api.pagination import decode_cursor
from core.models import Alert, CatalogMatch
from tests.factories import AlertFactory, CatalogMatchFactory, set_ingest_time

URL = '/api/recent-crossmatches'


@pytest.mark.django_db
def test_no_params_returns_200_default_window_and_detail(client):
    now = timezone.now()
    alert = AlertFactory(event_time=now)
    CatalogMatchFactory(alert=alert)
    set_ingest_time(alert, now - timedelta(hours=1))

    resp = client.get(URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body['detail'] == 'matches'
    assert body['time_field'] == 'ingest_time'
    assert body['count'] == 1
    assert 'matches' in body['objects'][0]


@pytest.mark.django_db
def test_reverse_matches_url():
    assert reverse('recent-crossmatches') == URL


@pytest.mark.django_db
def test_explicit_params_passed_through(client):
    now = timezone.now()
    alert = AlertFactory(event_time=now - timedelta(hours=2))
    CatalogMatchFactory(alert=alert)

    start = (now - timedelta(hours=6)).isoformat()
    end = now.isoformat()
    resp = client.get(
        URL,
        {'start': start, 'end': end, 'time_field': 'event_time', 'detail': 'ids'},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body['detail'] == 'ids'
    assert body['time_field'] == 'event_time'
    assert body['count'] == 1
    assert body['objects'][0] == {
        'diaObjectId': alert.lsst_diaObject_diaObjectId,
        'diaObjectId_str': str(alert.lsst_diaObject_diaObjectId),
    }


@pytest.mark.django_db
def test_invalid_detail_returns_400(client):
    resp = client.get(URL, {'detail': 'bogus'})
    assert resp.status_code == 400
    assert 'error' in resp.json()


@pytest.mark.django_db
def test_invalid_time_field_returns_400(client):
    resp = client.get(URL, {'time_field': 'created_at'})
    assert resp.status_code == 400
    assert 'error' in resp.json()


@pytest.mark.django_db
def test_unparseable_start_returns_400(client):
    resp = client.get(URL, {'start': 'not-a-timestamp'})
    assert resp.status_code == 400
    assert 'error' in resp.json()


@pytest.mark.django_db
def test_non_integer_page_size_returns_400(client):
    resp = client.get(URL, {'page_size': 'abc'})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_zero_page_size_returns_400(client):
    resp = client.get(URL, {'page_size': '0'})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_malformed_cursor_returns_400(client):
    resp = client.get(URL, {'cursor': 'not-a-real-cursor!!'})
    assert resp.status_code == 400
    assert 'error' in resp.json()


@pytest.mark.django_db
def test_empty_cursor_returns_400(client):
    """An explicit empty ?cursor= is malformed, not a silent page 1."""
    resp = client.get(URL, {'cursor': ''})
    assert resp.status_code == 400
    assert 'error' in resp.json()


@pytest.mark.django_db
def test_window_span_over_max_returns_400(client):
    now = timezone.now()
    resp = client.get(
        URL,
        {'start': (now - timedelta(days=365)).isoformat(), 'end': now.isoformat()},
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_first_page_returns_cursor_and_page_size(client):
    """AE1: no cursor over a window with more than page_size objects -> a page
    plus next_cursor, with page_size echoed in the body."""
    now = timezone.now()
    for i in range(2):
        alert = AlertFactory(event_time=now)
        CatalogMatchFactory(alert=alert)
        set_ingest_time(alert, now - timedelta(hours=1) - timedelta(minutes=i))

    resp = client.get(URL, {'page_size': '1', 'detail': 'ids'})

    assert resp.status_code == 200
    body = resp.json()
    assert body['page_size'] == 1
    assert body['count'] == 1
    assert body['next_cursor'] is not None


@pytest.mark.django_db
def test_follow_cursor_across_two_gets_covers_set(client):
    """AE2: following next_cursor through the real URLconf yields disjoint pages
    that together cover the seeded set."""
    now = timezone.now()
    ids = []
    for i in range(3):
        alert = AlertFactory(event_time=now)
        CatalogMatchFactory(alert=alert)
        set_ingest_time(alert, now - timedelta(hours=1) - timedelta(minutes=i))
        ids.append(alert.lsst_diaObject_diaObjectId)

    seen = []
    params = {'page_size': '1', 'detail': 'ids'}
    for _ in range(10):
        body = client.get(URL, params).json()
        seen.extend(o['diaObjectId'] for o in body['objects'])
        if body['next_cursor'] is None:
            break
        params = {'page_size': '1', 'cursor': body['next_cursor']}

    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))


@pytest.mark.django_db
def test_cursor_conflict_returns_400(client):
    """AE5: a cursor plus a conflicting time_field -> 400 JSON error."""
    now = timezone.now()
    for i in range(2):
        alert = AlertFactory(event_time=now - timedelta(minutes=i))
        CatalogMatchFactory(alert=alert)

    first = client.get(
        URL, {'page_size': '1', 'detail': 'ids', 'time_field': 'event_time'}
    ).json()

    resp = client.get(
        URL, {'cursor': first['next_cursor'], 'time_field': 'ingest_time'}
    )
    assert resp.status_code == 400
    assert 'error' in resp.json()


@override_settings(RECENT_CROSSMATCH_MAX_PAGE_SIZE=1)
@pytest.mark.django_db
def test_oversized_page_size_is_clamped_not_rejected(client):
    now = timezone.now()
    for i in range(2):
        alert = AlertFactory(event_time=now)
        CatalogMatchFactory(alert=alert)
        set_ingest_time(alert, now - timedelta(hours=1) - timedelta(minutes=i))

    resp = client.get(URL, {'page_size': '100000', 'detail': 'ids'})

    assert resp.status_code == 200
    assert resp.json()['page_size'] == 1
    assert resp.json()['count'] == 1


@pytest.mark.django_db
def test_stray_limit_param_is_ignored(client):
    """The retired ``limit`` param no longer truncates the page."""
    now = timezone.now()
    for i in range(3):
        alert = AlertFactory(event_time=now)
        CatalogMatchFactory(alert=alert)
        set_ingest_time(alert, now - timedelta(hours=1) - timedelta(minutes=i))

    resp = client.get(URL, {'limit': '1', 'detail': 'ids'})

    assert resp.status_code == 200
    assert resp.json()['count'] == 3  # limit ignored; default page size covers all


@pytest.mark.django_db
def test_detail_absent_defaults_to_matches(client):
    resp = client.get(URL)
    assert resp.status_code == 200
    assert resp.json()['detail'] == 'matches'


@pytest.mark.django_db
def test_endpoint_responds_without_authentication(client):
    """R11: no login/permission decorator; DEV serves the endpoint unauthenticated
    (no redirect to a login page, no 401/403)."""
    resp = client.get(URL)
    assert resp.status_code == 200


@pytest.mark.django_db
def test_non_get_method_rejected(client):
    resp = client.post(URL)
    assert resp.status_code == 405


def test_healthz_returns_ok(client):
    resp = client.get('/healthz')
    assert resp.status_code == 200
    assert resp.json() == {'status': 'ok'}


# --- U9 / AE7: the endpoint gains the contract additively -------------------

# A fixed dataset, so a response can be compared with one captured from the
# code before U9 (tests/fixtures/recent_crossmatches_pre_u9.json). IDs sit
# above 2^53 so the integer diaObjectId is exercised where JSON float parsers
# lose precision.
AE7_BASE = datetime(2026, 9, 1, 12, 0, tzinfo=dt_timezone.utc)
AE7_IDS = (9_007_199_254_740_993, 9_007_199_254_741_003, 9_007_199_254_741_013)
AE7_WINDOW = {
    'start': (AE7_BASE - timedelta(hours=1)).isoformat(),
    'end': (AE7_BASE + timedelta(hours=1)).isoformat(),
}
AE7_FIXTURE = Path(__file__).parent / 'fixtures' / 'recent_crossmatches_pre_u9.json'


def _seed_ae7_objects() -> None:
    """Create the fixed AE7 dataset: three matched objects and one unmatched."""
    for i, oid in enumerate(AE7_IDS):
        when = AE7_BASE - timedelta(minutes=i)
        alert = AlertFactory(
            lsst_diaObject_diaObjectId=oid,
            lsst_diaSource_diaSourceId=8_100_000_000 + i,
            event_time=when,
            ra_deg=10.0 + i,
            dec_deg=-20.0 - i,
            status=Alert.Status.NOTIFIED,
        )
        set_ingest_time(alert, when)
        CatalogMatchFactory(
            alert=alert, catalog_name='gaia_dr3', catalog_source_id=f'10{i}',
            match_distance_arcsec=0.25 + i / 10, source_ra_deg=10.0001 + i,
            source_dec_deg=-20.0001 - i,
            catalog_payload={'phot_g_mean_mag': 18.5 + i, 'parallax': None},
        )
    # A second catalog on the first object, and a superseded match version on
    # the second (only the current version is served).
    first = Alert.objects.get(lsst_diaObject_diaObjectId=AE7_IDS[0])
    CatalogMatchFactory(
        alert=first, catalog_name='des_y6_gold', catalog_source_id='202',
        match_distance_arcsec=0.9, source_ra_deg=10.0002, source_dec_deg=-20.0002,
        catalog_payload={'mag_auto_g': 21.25},
    )
    second = Alert.objects.get(lsst_diaObject_diaObjectId=AE7_IDS[1])
    CatalogMatchFactory(
        alert=second, catalog_name='gaia_dr3', catalog_source_id='101',
        match_distance_arcsec=0.4, source_ra_deg=11.0001, source_dec_deg=-21.0001,
        catalog_payload={'phot_g_mean_mag': 19.0}, match_version=2,
    )
    unmatched = AlertFactory(
        lsst_diaObject_diaObjectId=9_007_199_254_741_023,
        event_time=AE7_BASE, status=Alert.Status.NOTIFIED,
    )
    set_ingest_time(unmatched, AE7_BASE)


#: The keys U9 adds to a pre-U9 response; nothing else may appear or change.
#: List indices are folded to ``[]`` so one entry covers every object.
ADDED_KEYS = {'$.provenance', '$.as_of', '$.objects[].diaObjectId_str'}


def _added_shape(added: set[str]) -> set[str]:
    """Fold list indices in added key paths so objects compare as one shape."""
    return {re.sub(r'\[\d+\]', '[]', path) for path in added}


def _load_ae7_fixture() -> dict:
    return json.loads(AE7_FIXTURE.read_text())


def _assert_keeps(old, new, path='$', added=None):
    """Assert ``new`` keeps every key of ``old`` with the same type and value.

    ``next_cursor`` is opaque: it must stay a string (or null where it was
    null), but its token now also carries the kind and the ``as_of`` pin, so
    its value is checked by following it instead. Keys ``new`` adds are
    collected in ``added``.
    """
    if isinstance(old, dict):
        assert isinstance(new, dict), path
        for key, value in old.items():
            assert key in new, f'{path}.{key} was removed'
            if key == 'next_cursor' and value is not None:
                assert isinstance(new[key], str), f'{path}.{key}'
                continue
            _assert_keeps(value, new[key], f'{path}.{key}', added)
        if added is not None:
            added.update(f'{path}.{key}' for key in new if key not in old)
    elif isinstance(old, list):
        assert isinstance(new, list) and len(new) == len(old), path
        for i, (o, n) in enumerate(zip(old, new)):
            _assert_keeps(o, n, f'{path}[{i}]', added)
    else:
        assert type(new) is type(old), f'{path}: {type(old)} -> {type(new)}'
        assert new == old, f'{path}: {old!r} -> {new!r}'


@pytest.mark.django_db
def test_ae7_every_pre_u9_key_keeps_its_type_and_value(client, openapi_validate):
    """AE7: against a response captured before U9, every existing key is still
    present with the same type and value; only provenance and as_of are added.
    Following the new next_cursor yields the captured second page."""
    _seed_ae7_objects()
    fixture = _load_ae7_fixture()
    assert len(fixture['pages']) == 8  # both time fields x four detail levels

    for name, captured in fixture['pages'].items():
        first = client.get(URL, captured['params'])
        assert first.status_code == 200, name
        added: set[str] = set()
        _assert_keeps(captured['first'], first.json(), added=added)
        assert _added_shape(added) == ADDED_KEYS, name
        openapi_validate('recent_crossmatches', 200, first.json())

        second = client.get(URL, {'cursor': first.json()['next_cursor'], 'page_size': '2'})
        assert second.status_code == 200, name
        added = set()
        _assert_keeps(captured['second'], second.json(), added=added)
        assert _added_shape(added) == ADDED_KEYS, name
        assert second.json()['as_of'] == first.json()['as_of']
        openapi_validate('recent_crossmatches', 200, second.json())


@pytest.mark.django_db
def test_ae7_cursor_minted_before_the_upgrade_continues_the_walk(client, openapi_validate):
    """A next_cursor captured from the pre-U9 code (no as_of pin) resumes the
    walk at the same place and returns the captured second page."""
    _seed_ae7_objects()
    for name, captured in _load_ae7_fixture()['pages'].items():
        old_cursor = captured['first']['next_cursor']
        assert decode_cursor(old_cursor).as_of is None  # really pre-upgrade

        resp = client.get(URL, {'cursor': old_cursor, 'page_size': '2'})

        assert resp.status_code == 200, name
        added: set[str] = set()
        _assert_keeps(captured['second'], resp.json(), added=added)
        assert _added_shape(added) == ADDED_KEYS, name
        openapi_validate('recent_crossmatches', 200, resp.json())


@pytest.mark.django_db
def test_ae7_error_bodies_keep_error_and_add_structured_keys(client, openapi_validate):
    """KTD2 / A4: every 400 and 405 keeps its status and its ``error`` string
    (same text), and adds ``code``, ``message``, and ``retryable``."""
    for name, captured in _load_ae7_fixture()['errors'].items():
        if captured['params'] is None:
            resp = client.post(URL)
        else:
            resp = client.get(URL, captured['params'])

        assert resp.status_code == captured['status'], name
        body = resp.json()
        assert body['error'] == captured['body']['error'], name
        assert body['message'] == body['error']
        assert body['retryable'] is False
        expected = 'method_not_allowed' if resp.status_code == 405 else 'invalid_parameter'
        assert body['code'] == expected, name
        openapi_validate('recent_crossmatches', resp.status_code, body)


@pytest.mark.django_db
def test_invalid_parameter_errors_name_the_parameter(client):
    cases = {
        'detail': {'detail': 'bogus'},
        'time_field': {'time_field': 'created_at'},
        'start': {'start': 'not-a-timestamp'},
        'end': {'end': 'not-a-timestamp'},
        'page_size': {'page_size': 'abc'},
        'cursor': {'cursor': 'not-a-real-cursor!!'},
    }
    for param, params in cases.items():
        body = client.get(URL, params).json()
        assert body['param'] == param, params


@pytest.mark.django_db
def test_database_outage_is_a_structured_503(client, monkeypatch, openapi_validate):
    """KTD12: the guard turns a lost database connection into a retryable 503."""
    def lost(*args, **kwargs):
        raise OperationalError('server closed the connection unexpectedly')

    monkeypatch.setattr('api.service._load_matches', lost)
    CatalogMatchFactory(alert=AlertFactory())

    resp = client.get(URL)

    assert resp.status_code == 503
    assert int(resp['Retry-After']) > 0
    body = resp.json()
    assert body['code'] == 'service_unavailable'
    assert body['retryable'] is True
    assert body['error'] == body['message']
    openapi_validate('recent_crossmatches', 503, body)


@pytest.mark.django_db(transaction=True)
@override_settings(API_REQUEST_BUDGET_SECONDS=0.5)
def test_overrun_is_a_structured_400_query_too_expensive(client, monkeypatch, openapi_validate):
    """KTD12: a statement that would outrun the budget is cancelled by Postgres
    and answered 400 query_too_expensive (not retryable)."""
    def slow(*args, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_sleep(10)')

    monkeypatch.setattr('api.service._load_matches', slow)
    CatalogMatchFactory(alert=AlertFactory())

    started = time.monotonic()
    resp = client.get(URL)

    assert time.monotonic() - started < 3.0
    assert resp.status_code == 400
    body = resp.json()
    assert body['code'] == 'query_too_expensive'
    assert body['retryable'] is False
    assert body['error'] == body['message']
    openapi_validate('recent_crossmatches', 400, body)


@pytest.mark.django_db
def test_default_full_page_fits_the_default_budget(client, openapi_validate):
    """KTD12: a full default-size page at detail=full answers within the default
    request budget (5 s) on the test database."""
    now = timezone.now()
    size = settings.RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE
    alerts = Alert.objects.bulk_create([
        Alert(
            lsst_diaObject_diaObjectId=9_500_000_000 + i,
            lsst_diaSource_diaSourceId=9_600_000_000 + i,
            ra_deg=180.0, dec_deg=-30.0, event_time=now - timedelta(seconds=i),
            schema_version=1, payload={}, status=Alert.Status.NOTIFIED,
        )
        for i in range(size)
    ])
    CatalogMatch.objects.bulk_create([
        CatalogMatch(
            alert=alert, catalog_name='gaia_dr3', catalog_source_id=str(i),
            match_distance_arcsec=0.5, source_ra_deg=180.0, source_dec_deg=-30.0,
            catalog_payload={'phot_g_mean_mag': 18.0}, match_version=1,
        )
        for i, alert in enumerate(alerts)
    ])

    resp = client.get(URL, {'detail': 'full', 'time_field': 'event_time'})

    assert resp.status_code == 200
    assert resp.json()['count'] == size
    openapi_validate('recent_crossmatches', 200, resp.json())


def test_api_docs_page_shows_the_additive_contract(client):
    """The HTML reference's recent-crossmatches examples match the served
    bodies: provenance and as_of on a page, structured keys on errors."""
    body = client.get(reverse('web:api')).content.decode()
    for text in (
        '&quot;as_of&quot;', '&quot;provenance&quot;',
        '&quot;code&quot;: &quot;invalid_parameter&quot;',
        '&quot;code&quot;: &quot;method_not_allowed&quot;',
        '&quot;code&quot;: &quot;service_unavailable&quot;',
        'unsupported_parameter', 'query_too_expensive',
    ):
        assert text in body or text.replace('&quot;', '"') in body, text
