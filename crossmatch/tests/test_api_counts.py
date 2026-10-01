"""U6: ``response=count`` on the new queries (R15; KTD10; AE3).

Counts share the query path: the same inputs and filters as a listing, answered
with counts by input status, object status, and ``qualifies`` instead of
objects. Every response validates against the served OpenAPI document.
"""

import json

import pytest
from django.test import override_settings
from django.utils import timezone

from api.contract import InputStatus, ObjectStatus, QualifiesReason
from core.healpix import radec_to_ipix
from core.models import Alert, TnsObject, TnsSnapshotMeta
from tests.factories import (
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

LOOKUP = '/api/lookup'
CONE = '/api/cone'
ARCSEC = 1.0 / 3600.0
POE_MIN = 'gaia_dr3.parallax_over_error_min'


def _post(client, body):
    return client.post(LOOKUP, data=json.dumps(body), content_type='application/json')


def _id(object_id):
    return {'kind': 'id', 'diaObjectId': int(object_id)}


def _alert(ra=180.0, dec=-30.0, **kwargs):
    return AlertFactory(ra_deg=ra, dec_deg=dec, healpix_ipix=radec_to_ipix(ra, dec), **kwargs)


def matched(payload, catalog='gaia_dr3', ra=180.0, dec=-30.0):
    record = ObjectCrossmatchRecordFactory(
        alert=_alert(ra, dec, status=Alert.Status.MATCHED)
    )
    CatalogMatchFactory(alert=record.alert, catalog_name=catalog, catalog_payload=payload)
    return record.alert.lsst_diaObject_diaObjectId


def no_match():
    return ObjectCrossmatchRecordFactory(
        alert=_alert(status=Alert.Status.MATCHED)
    ).alert.lsst_diaObject_diaObjectId


@pytest.mark.django_db
def test_ae3_count_of_300_candidates_with_significant_parallax(client, openapi_validate):
    """AE3: 12 of 300 IDs have a Gaia source with parallax_over_error > 5."""
    ids = []
    ids += [matched({'parallax': 6.0 + i, 'parallax_error': 1.0}) for i in range(12)]
    ids += [matched({'parallax': 2.0, 'parallax_error': 1.0}) for _ in range(20)]
    ids += [matched({'dnf_z': 0.2}, catalog='des_y6_gold') for _ in range(8)]
    ids += [matched({'parallax': None, 'parallax_error': 1.0}) for _ in range(5)]
    ids += [no_match() for _ in range(200)]
    ids += [_alert().lsst_diaObject_diaObjectId for _ in range(25)]
    ids += [5_000_000_000 + i for i in range(30)]  # never seen
    assert len(ids) == 300

    resp = _post(client, {
        'inputs': [_id(i) for i in ids],
        'filters': {POE_MIN: 5},
        'response': 'count',
    })

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    assert 'results' not in body
    assert body['response'] == 'count'
    assert body['filters'] == {POE_MIN: 5.0}
    objects = body['counts']['objects']
    assert objects['total'] == 300
    assert objects['qualifying'] == 12
    assert objects['by_status'][ObjectStatus.COINCIDENT_SOURCES] == 45
    assert objects['by_status'][ObjectStatus.NO_COINCIDENT_SOURCE] == 200
    assert objects['by_status'][ObjectStatus.CROSSMATCH_PENDING] == 25
    assert objects['by_status'][ObjectStatus.NOT_IN_SERVICE] == 30
    assert objects['by_status'][ObjectStatus.NOT_SEARCHED] == 0
    assert objects['by_qualifies_reason'] == {
        QualifiesReason.MEETS_FILTERS: 12,
        QualifiesReason.NO_SOURCE_MEETS_FILTERS: 33,
        QualifiesReason.NO_COINCIDENT_SOURCE: 200,
        QualifiesReason.CROSSMATCH_PENDING: 25,
        QualifiesReason.NOT_IN_SERVICE: 30,
        QualifiesReason.NOT_SEARCHED: 0,
        QualifiesReason.OBJECT_FILTERS_NOT_MET: 0,
    }
    inputs = body['counts']['inputs']
    assert inputs['total'] == 300
    assert inputs['qualifying'] == 12
    assert inputs['by_status'][ObjectStatus.COINCIDENT_SOURCES] == 45


@pytest.mark.django_db
def test_count_matches_the_listing_for_the_same_inputs(client, openapi_validate):
    ids = [
        matched({'parallax': 12.0, 'parallax_error': 1.0}),
        matched({'parallax': 1.0, 'parallax_error': 1.0}),
        no_match(),
    ]
    inputs = [_id(i) for i in ids] + [_id(ids[0]), {'kind': 'id', 'diaObjectId': 'bad'}]

    listing = _post(client, {'inputs': inputs, 'filters': {POE_MIN: 5}}).json()
    count = _post(client, {'inputs': inputs, 'filters': {POE_MIN: 5}, 'response': 'count'})

    assert count.status_code == 200
    openapi_validate('lookup_objects', 200, count.json())
    counts = count.json()['counts']
    assert counts['inputs']['total'] == len(inputs)
    assert counts['inputs']['qualifying'] == sum(r['qualifies'] for r in listing['results'])
    assert counts['inputs']['by_status'][InputStatus.INVALID_INPUT] == 1
    # Duplicate inputs are counted per input; objects are counted once.
    assert counts['objects']['total'] == 3
    assert counts['objects']['qualifying'] == 1


@pytest.mark.django_db
def test_unfiltered_count_reports_statuses_without_qualification(client, openapi_validate):
    ids = [matched({'parallax': 1.0}), no_match()]

    resp = _post(client, {'inputs': [_id(i) for i in ids], 'response': 'count'})

    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    assert body['filters'] is None
    assert body['counts']['objects']['qualifying'] is None
    assert body['counts']['objects']['by_qualifies_reason'] is None
    assert body['counts']['inputs']['qualifying'] is None
    assert body['counts']['objects']['by_status'][ObjectStatus.COINCIDENT_SOURCES] == 1


@pytest.mark.django_db
def test_response_must_be_objects_or_count(client, openapi_validate):
    resp = _post(client, {'inputs': [_id(1)], 'response': 'total'})

    assert resp.status_code == 400
    openapi_validate('lookup_objects', 400, resp.json())
    assert resp.json()['param'] == 'response'


@pytest.mark.django_db
def test_response_objects_is_the_default_listing(client, openapi_validate):
    resp = _post(client, {'inputs': [_id(1)], 'response': 'objects'})

    assert resp.status_code == 200
    openapi_validate('lookup_objects', 200, resp.json())
    assert len(resp.json()['results']) == 1


@override_settings(API_MAX_OBJECTS_PER_POSITION=2, API_MAX_OBJECTS_PER_REQUEST=3)
@pytest.mark.django_db
def test_position_count_covers_every_object_past_the_listing_caps(client, openapi_validate):
    for step in range(5):
        matched({'parallax': 12.0, 'parallax_error': 1.0}, ra=150.0, dec=2.0 + step * ARCSEC)
    _alert(150.0, 2.0 - ARCSEC)
    position = {'kind': 'position', 'ra': 150.0, 'dec': 2.0, 'radius_arcsec': 10}

    resp = _post(client, {'inputs': [position], 'filters': {POE_MIN: 5}, 'response': 'count'})

    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    assert body['counts']['inputs']['by_status'][InputStatus.OBJECTS_FOUND] == 1
    assert body['counts']['inputs']['qualifying'] == 1
    assert body['counts']['objects']['total'] == 6
    assert body['counts']['objects']['qualifying'] == 5
    assert body['counts']['objects']['by_status'][ObjectStatus.CROSSMATCH_PENDING] == 1


@pytest.mark.django_db
def test_cone_count(client, openapi_validate):
    matched({'parallax': 12.0, 'parallax_error': 1.0}, ra=150.0, dec=2.0)
    matched({'parallax': 1.0, 'parallax_error': 1.0}, ra=150.0, dec=2.0 + ARCSEC)

    resp = client.get(CONE, {
        'ra': '150', 'dec': '2', 'radius_arcsec': '5', 'response': 'count', POE_MIN: '5',
    })

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('cone_search', 200, body)
    assert body['counts']['inputs'] == {
        'total': 1,
        'by_status': {**body['counts']['inputs']['by_status'], InputStatus.OBJECTS_FOUND: 1},
        'qualifying': 1,
    }
    assert body['counts']['objects']['total'] == 2
    assert body['counts']['objects']['qualifying'] == 1


@pytest.mark.django_db
def test_cone_count_takes_no_cursor(client, openapi_validate):
    resp = client.get(CONE, {'cursor': 'abc', 'response': 'count'})

    assert resp.status_code == 400
    openapi_validate('cone_search', 400, resp.json())
    assert resp.json()['param'] == 'cursor'


@pytest.mark.django_db
def test_tns_and_object_counts(client, openapi_validate):
    oid = matched({'parallax': 12.0, 'parallax_error': 1.0}, ra=150.0, dec=2.0)
    TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())
    TnsObject.objects.create(
        objid=1, name='2026abc', name_prefix='SN', ra_deg=150.0, dec_deg=2.0,
        type='SN Ia', redshift=0.05, healpix_ipix=radec_to_ipix(150.0, 2.0),
    )

    tns = client.get('/api/tns/2026abc', {'response': 'count', POE_MIN: '5'})
    obj = client.get(f'/api/objects/{oid}', {'response': 'count', POE_MIN: '5'})

    for resp, operation in ((tns, 'resolve_tns'), (obj, 'get_object')):
        assert resp.status_code == 200
        openapi_validate(operation, 200, resp.json())
        assert resp.json()['counts']['objects']['qualifying'] == 1
        assert resp.json()['counts']['inputs']['qualifying'] == 1


@pytest.mark.django_db
def test_count_for_unresolved_tns_name(client, openapi_validate):
    TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())

    resp = client.get('/api/tns/2026zzz', {'response': 'count'})

    body = resp.json()
    openapi_validate('resolve_tns', 200, body)
    assert body['counts']['inputs']['by_status'][InputStatus.TNS_NAME_NOT_FOUND] == 1
    assert body['counts']['objects']['total'] == 0
