"""U5: the cone search HTTP views, ``GET api/cone`` and positions in ``POST api/lookup``.

Parameter parsing, 400s naming the parameter, paging over HTTP, and
conformance of every response to the served OpenAPI document (the
``openapi_validate`` fixture). Query behavior is covered by
``test_cone_search_service``.
"""

import json

import pytest
from django.test import override_settings
from django.urls import reverse

from api.contract import InputStatus, ObjectStatus
from core.healpix import radec_to_ipix
from core.models import Alert
from tests.factories import (
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

CONE = '/api/cone'
LOOKUP = '/api/lookup'
ARCSEC = 1.0 / 3600.0


def alert_at(ra, dec, **kwargs):
    return AlertFactory(
        ra_deg=ra, dec_deg=dec, healpix_ipix=radec_to_ipix(ra, dec), **kwargs
    )


def _post(client, body):
    return client.post(LOOKUP, data=json.dumps(body), content_type='application/json')


def test_cone_route_is_slashless():
    assert reverse('cone-search') == CONE


@pytest.mark.parametrize('detail', ['ids', 'position', 'matches', 'full'])
@pytest.mark.django_db
def test_cone_with_pending_and_matched_objects_validates(client, openapi_validate, detail):
    alert_at(150.0, 2.0)
    record = ObjectCrossmatchRecordFactory(
        alert=alert_at(150.0, 2.0 + ARCSEC, status=Alert.Status.MATCHED)
    )
    CatalogMatchFactory(alert=record.alert, catalog_payload={'parallax': 1.5})

    resp = client.get(CONE, {'ra': '150', 'dec': '2', 'radius_arcsec': '5', 'detail': detail})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('cone_search', 200, body)
    result = body['results'][0]
    assert result['status'] == InputStatus.OBJECTS_FOUND
    assert sorted(obj['status'] for obj in result['objects']) == sorted([
        ObjectStatus.CROSSMATCH_PENDING, ObjectStatus.COINCIDENT_SOURCES,
    ])


@pytest.mark.django_db
def test_empty_cone_validates(client, openapi_validate):
    # Covers AE2 over HTTP.
    resp = client.get(CONE, {'ra': '150', 'dec': '2', 'radius_arcsec': '2'})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('cone_search', 200, body)
    assert body['results'][0]['status'] == InputStatus.NO_RUBIN_OBJECT
    assert body['results'][0]['input'] == {'ra': '150', 'dec': '2', 'radius_arcsec': '2'}


@pytest.mark.django_db
def test_cone_pages_over_http(client, openapi_validate):
    for k in range(3):
        alert_at(150.0, 2.0 + k * ARCSEC)

    seen = []
    params = {'ra': '150', 'dec': '2', 'radius_arcsec': '10', 'page_size': '2'}
    while True:
        resp = client.get(CONE, params)
        assert resp.status_code == 200
        body = resp.json()
        openapi_validate('cone_search', 200, body)
        seen.extend(obj['diaObjectId'] for obj in body['results'][0]['objects'])
        if body['next_cursor'] is None:
            break
        params = {'cursor': body['next_cursor'], 'page_size': '2'}

    assert len(seen) == len(set(seen)) == 3


@pytest.mark.parametrize('params, param', [
    ({'dec': '2', 'radius_arcsec': '2'}, 'ra'),
    ({'ra': 'abc', 'dec': '2', 'radius_arcsec': '2'}, 'ra'),
    ({'ra': 'nan', 'dec': '2', 'radius_arcsec': '2'}, 'ra'),
    ({'ra': '10', 'dec': '-91', 'radius_arcsec': '2'}, 'dec'),
    ({'ra': '10', 'dec': '2'}, 'radius_arcsec'),
    ({'ra': '10', 'dec': '2', 'radius_arcsec': '61'}, 'radius_arcsec'),
    ({'ra': '10', 'dec': '2', 'radius_arcsec': '-1'}, 'radius_arcsec'),
    ({'ra': '10', 'dec': '2', 'radius_arcsec': '2', 'detail': 'bogus'}, 'detail'),
    ({'ra': '10', 'dec': '2', 'radius_arcsec': '2', 'page_size': 'x'}, 'page_size'),
    ({'ra': '10', 'dec': '2', 'radius_arcsec': '2', 'page_size': '0'}, 'page_size'),
    # isdigit() is true for a superscript digit, but int() rejects it.
    ({'ra': '10', 'dec': '2', 'radius_arcsec': '2', 'page_size': '\u00b2'}, 'page_size'),
    ({'cursor': 'garbage!!'}, 'cursor'),
])
@pytest.mark.django_db
@override_settings(API_MAX_CONE_RADIUS_ARCSEC=60)
def test_bad_cone_parameter_is_400_naming_it(client, openapi_validate, params, param):
    resp = client.get(CONE, params)

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('cone_search', 400, body)
    assert body['code'] == 'invalid_parameter'
    assert body['param'] == param


@pytest.mark.django_db
def test_recent_crossmatches_cursor_is_rejected_by_cone(client):
    from datetime import datetime, timezone

    from api.pagination import Cursor, encode_cursor
    token = encode_cursor(Cursor(
        time_field_value=datetime(2026, 1, 1, tzinfo=timezone.utc), dia_object_id=1,
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        time_field='ingest_time', detail='matches',
    ))

    resp = client.get(CONE, {'cursor': token})

    assert resp.status_code == 400
    assert resp.json()['param'] == 'cursor'


@pytest.mark.django_db
def test_cone_rejects_post(client, openapi_validate):
    resp = client.post(CONE)
    assert resp.status_code == 405
    openapi_validate('cone_search', 405, resp.json())


@pytest.mark.django_db
@override_settings(API_MAX_OBJECTS_PER_POSITION=1)
def test_batched_positions_with_truncation_validate(client, openapi_validate):
    alert_at(150.0, 2.0)
    alert_at(150.0, 2.0 + ARCSEC)

    resp = _post(client, {
        'inputs': [
            {'kind': 'position', 'ra': 150.0, 'dec': 2.0},
            {'kind': 'position', 'ra': 10.0, 'dec': 2.0, 'radius_arcsec': 1},
            {'kind': 'position', 'ra': 10.0, 'dec': 99.0, 'radius_arcsec': 1},
        ],
        'radius_arcsec': 5,
        'detail': 'full',
    })

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    truncated, empty, bad = body['results']
    assert truncated['truncated'] is True and truncated['total'] == 2
    assert empty['status'] == InputStatus.NO_RUBIN_OBJECT
    assert bad['status'] == InputStatus.INVALID_INPUT
    assert bad['error']['param'] == 'inputs[2].dec'


@pytest.mark.django_db
@override_settings(API_MAX_CONE_RADIUS_ARCSEC=60)
def test_lookup_shared_radius_over_the_maximum_is_400(client, openapi_validate):
    resp = _post(client, {
        'inputs': [{'kind': 'position', 'ra': 150.0, 'dec': 2.0}], 'radius_arcsec': 90,
    })

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('lookup_objects', 400, body)
    assert body['param'] == 'radius_arcsec'


def test_openapi_states_null_healpix_objects_are_unreachable(client):
    doc = client.get('/openapi.json').json()
    description = doc['paths']['/api/cone']['get']['description']
    assert 'HEALPix' in description
    assert 'not reachable' in description
