"""U4: the object lookup HTTP views, ``GET api/objects/<id>`` and ``POST api/lookup``.

Request parsing, request-level 400s naming the parameter (R28), method
handling, and conformance of every response to the served OpenAPI document
(the ``openapi_validate`` fixture). Query behavior is covered by
``test_object_lookup_service``.
"""

import json

import pytest
from django.test import override_settings
from django.urls import reverse

from api.contract import InputStatus, ObjectStatus
from core.models import Alert
from tests.factories import (
    AlertDeliveryFactory,
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

LOOKUP = '/api/lookup'
UNKNOWN_ID = 4_242_424_242


def _object_url(object_id):
    return f'/api/objects/{object_id}'


def _post(client, body, content_type='application/json'):
    data = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return client.post(LOOKUP, data=data, content_type=content_type)


def _id(object_id):
    return {'kind': 'id', 'diaObjectId': object_id}


def test_routes_are_slashless():
    assert reverse('lookup-objects') == LOOKUP
    assert reverse('get-object', args=['123']) == '/api/objects/123'


@pytest.mark.parametrize('detail', ['ids', 'position', 'matches', 'full'])
@pytest.mark.django_db
def test_get_matched_object_at_each_detail_level(client, openapi_validate, detail):
    record = ObjectCrossmatchRecordFactory()
    AlertDeliveryFactory(alert=record.alert)
    CatalogMatchFactory(
        alert=record.alert, catalog_name='gaia_dr3',
        catalog_payload={'parallax': 1.5, 'ruwe': None},
    )

    resp = client.get(_object_url(record.alert.lsst_diaObject_diaObjectId), {'detail': detail})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('get_object', 200, body)
    assert body['detail'] == detail
    assert body['results'][0]['status'] == ObjectStatus.COINCIDENT_SOURCES


@pytest.mark.django_db
def test_get_unknown_object_is_not_in_service(client, openapi_validate):
    resp = client.get(_object_url(UNKNOWN_ID))

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('get_object', 200, body)
    assert body['results'][0]['status'] == ObjectStatus.NOT_IN_SERVICE
    assert body['results'][0]['input'] == str(UNKNOWN_ID)


@pytest.mark.django_db
def test_get_unrecorded_object_validates(client, openapi_validate):
    alert = AlertFactory(status=Alert.Status.NOTIFIED)
    CatalogMatchFactory(alert=alert)

    resp = client.get(_object_url(alert.lsst_diaObject_diaObjectId), {'detail': 'full'})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('get_object', 200, body)
    assert body['results'][0]['objects'][0]['crossmatch']['provenance'] == 'not_recorded'


@pytest.mark.parametrize('raw', ['12x', '-5', '99999999999999999999'])
@pytest.mark.django_db
def test_get_malformed_id_is_400_naming_the_param(client, openapi_validate, raw):
    resp = client.get(_object_url(raw))

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('get_object', 400, body)
    assert body['code'] == 'invalid_parameter'
    assert body['param'] == 'diaObjectId'
    assert body['error'] == body['message']


@pytest.mark.django_db
def test_get_bad_detail_is_400_naming_detail(client, openapi_validate):
    resp = client.get(_object_url(1), {'detail': 'everything'})

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('get_object', 400, body)
    assert body['param'] == 'detail'


@pytest.mark.django_db
def test_post_to_object_route_is_405(client, openapi_validate):
    resp = client.post(_object_url(1))

    assert resp.status_code == 405
    openapi_validate('get_object', 405, resp.json())


@pytest.mark.django_db
def test_lookup_ae1_batch(client, openapi_validate):
    pending = AlertFactory(status=Alert.Status.INGESTED)
    no_match = ObjectCrossmatchRecordFactory().alert
    gaia = ObjectCrossmatchRecordFactory().alert
    CatalogMatchFactory(alert=gaia, catalog_name='gaia_dr3', catalog_payload={'parallax': 3.2})

    resp = _post(client, {
        'inputs': [
            _id(UNKNOWN_ID),
            _id(pending.lsst_diaObject_diaObjectId),
            _id(no_match.lsst_diaObject_diaObjectId),
            _id(gaia.lsst_diaObject_diaObjectId),
        ],
        'detail': 'full',
    })

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    assert [r['status'] for r in body['results']] == [
        ObjectStatus.NOT_IN_SERVICE,
        ObjectStatus.CROSSMATCH_PENDING,
        ObjectStatus.NO_COINCIDENT_SOURCE,
        ObjectStatus.COINCIDENT_SOURCES,
    ]


@pytest.mark.django_db
def test_lookup_mixed_batch_with_malformed_and_duplicate_entries(client, openapi_validate):
    alert = ObjectCrossmatchRecordFactory().alert
    CatalogMatchFactory(alert=alert)
    oid = alert.lsst_diaObject_diaObjectId
    inputs = [
        _id(oid), 'junk', {'kind': 'bogus'}, {'kind': 'id', 'diaObjectId': 1.5},
        _id(str(oid)), _id(oid),
    ]

    resp = _post(client, {'inputs': inputs})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    assert [r['input'] for r in body['results']] == inputs
    assert [r['status'] for r in body['results']] == [
        ObjectStatus.COINCIDENT_SOURCES,
        InputStatus.INVALID_INPUT,
        InputStatus.INVALID_INPUT,
        InputStatus.INVALID_INPUT,
        ObjectStatus.COINCIDENT_SOURCES,
        ObjectStatus.COINCIDENT_SOURCES,
    ]


@pytest.mark.django_db
def test_lookup_id_above_2_pow_53_round_trips_through_json(client, openapi_validate):
    big = 2**63 - 7
    AlertFactory(lsst_diaObject_diaObjectId=big, status=Alert.Status.MATCHED)

    resp = client.post(
        LOOKUP,
        data='{"inputs": [{"kind": "id", "diaObjectId": "%d"}]}' % big,
        content_type='application/json',
    )

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    # The raw text carries the exact digits, so a float-parsing client can use
    # diaObjectId_str.
    assert f'"diaObjectId_str": "{big}"' in resp.content.decode()
    obj = body['results'][0]['objects'][0]
    assert obj['diaObjectId'] == big
    assert obj['diaObjectId_str'] == str(big)


@pytest.mark.django_db
@override_settings(API_MAX_IDS=2)
def test_lookup_over_the_id_maximum_is_400_naming_inputs(client, openapi_validate):
    resp = _post(client, {'inputs': [_id(1), _id(2), _id(3)]})

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('lookup_objects', 400, body)
    assert body['param'] == 'inputs'


@pytest.mark.parametrize('body, param', [
    ({'inputs': []}, 'inputs'),
    ({'inputs': {'kind': 'id'}}, 'inputs'),
    ({}, 'inputs'),
    ({'inputs': [{'kind': 'id', 'diaObjectId': 1}], 'detail': 'bogus'}, 'detail'),
    ({'inputs': [{'kind': 'id', 'diaObjectId': 1}], 'radius': 5}, 'radius'),
    ([{'kind': 'id', 'diaObjectId': 1}], 'body'),
    ('"a string"', 'body'),
])
@pytest.mark.django_db
def test_lookup_request_level_problems_are_400_naming_the_param(
    client, openapi_validate, body, param,
):
    resp = _post(client, body)

    assert resp.status_code == 400
    err = resp.json()
    openapi_validate('lookup_objects', 400, err)
    assert err['code'] == 'invalid_parameter'
    assert err['param'] == param


@pytest.mark.parametrize('raw', ['{"inputs": [', ' ', 'not json', b'\xff\xfe', 'NaN'])
@pytest.mark.django_db
def test_lookup_malformed_json_is_400_naming_body(client, openapi_validate, raw):
    resp = _post(client, raw)

    assert resp.status_code == 400
    err = resp.json()
    openapi_validate('lookup_objects', 400, err)
    assert err['param'] == 'body'


@pytest.mark.parametrize('content_type', [
    'text/plain', 'application/x-www-form-urlencoded', 'application/jsonx',
])
@pytest.mark.django_db
def test_lookup_wrong_content_type_is_400(client, openapi_validate, content_type):
    resp = _post(client, {'inputs': [_id(1)]}, content_type=content_type)

    assert resp.status_code == 400
    err = resp.json()
    openapi_validate('lookup_objects', 400, err)
    assert err['param'] == 'Content-Type'


@pytest.mark.django_db
def test_lookup_accepts_json_with_charset(client, openapi_validate):
    resp = _post(
        client, {'inputs': [_id(1)]}, content_type='application/json; charset=utf-8',
    )

    assert resp.status_code == 200
    openapi_validate('lookup_objects', 200, resp.json())


@pytest.mark.django_db
@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=64)
def test_lookup_oversized_body_is_400_naming_body(client, openapi_validate):
    resp = _post(client, {'inputs': [_id(n) for n in range(50)]})

    assert resp.status_code == 400
    err = resp.json()
    openapi_validate('lookup_objects', 400, err)
    assert err['param'] == 'body'


@pytest.mark.django_db
def test_get_on_lookup_is_405(client, openapi_validate):
    resp = client.get(LOOKUP)

    assert resp.status_code == 405
    openapi_validate('lookup_objects', 405, resp.json())
