"""U5: TNS-name inputs, ``GET api/tns/<name>`` and the ``tns`` kind (R3, R21, R31).

Covers AE5 and AE6, name normalization, the snapshot-currency helper shared
with the crossmatch task, and conformance of every response to the served
OpenAPI document.
"""

import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from api.contract import InputStatus, ObjectStatus
from api.errors import InvalidQuery
from api.lookup import lookup_objects
from api.positions import normalize_tns_name, resolve_tns
from core.healpix import radec_to_ipix
from core.models import Alert, TnsAssociation, TnsObject, TnsSnapshotMeta
from tests.factories import (
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

ARCSEC = 1.0 / 3600.0
LOOKUP = '/api/lookup'


def alert_at(ra, dec, **kwargs):
    return AlertFactory(
        ra_deg=ra, dec_deg=dec, healpix_ipix=radec_to_ipix(ra, dec), **kwargs
    )


def tns_object(name, ra, dec, **kwargs):
    fields = dict(
        objid=kwargs.pop('objid', abs(hash(name)) % 10**9),
        name=name, name_prefix='SN', ra_deg=ra, dec_deg=dec, type='SN Ia',
        redshift=0.05, healpix_ipix=radec_to_ipix(ra, dec),
    )
    fields.update(kwargs)
    return TnsObject.objects.create(**fields)


def current_snapshot():
    return TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())


def tns(name, radius=None):
    entry = {'kind': 'tns', 'name': name}
    if radius is not None:
        entry['radius_arcsec'] = radius
    return entry


def only_result(body):
    assert len(body['results']) == 1
    return body['results'][0]


def ids_of(result):
    return [obj['diaObjectId'] for obj in result['objects']]


# --- normalization (KTD13) ---

@pytest.mark.parametrize('raw, expected', [
    ('SN 2026abc', '2026abc'),
    ('2026ABC', '2026abc'),
    ('AT2026abc', '2026abc'),
    ('  at 2026abc ', '2026abc'),
    ('sn2026A', '2026a'),
    ('2026a', '2026a'),
    ('SN 2026 abc', '2026abc'),
])
def test_normalize_tns_name(raw, expected):
    assert normalize_tns_name(raw) == expected


@pytest.mark.parametrize('raw', ['', '   ', 'SN', '2026abc/x', 'x' * 65, 5, None])
def test_normalize_rejects_malformed_names(raw):
    with pytest.raises(ValueError):
        normalize_tns_name(raw)


# --- resolution ---

@pytest.mark.django_db
def test_name_variants_resolve_to_the_same_record():
    current_snapshot()
    record = tns_object('2026abc', 150.0, 2.0)
    obj = alert_at(150.0, 2.0 + 0.2 * ARCSEC)

    body = lookup_objects(inputs=[tns('SN 2026abc'), tns('2026ABC'), tns('AT2026abc')])

    for result in body['results']:
        assert result['status'] == InputStatus.OBJECTS_FOUND
        assert result['normalized']['name'] == '2026abc'
        assert result['tns']['objid'] == record.objid
        assert result['tns']['name'] == '2026abc'
        assert ids_of(result) == [obj.lsst_diaObject_diaObjectId]


@pytest.mark.django_db
def test_lowercase_input_finds_uppercase_single_letter_designation():
    current_snapshot()
    record = tns_object('2026A', 10.0, -5.0)

    result = only_result(resolve_tns(name='2026a'))

    assert result['tns']['objid'] == record.objid
    assert result['tns']['name'] == '2026A'
    assert result['status'] == InputStatus.NO_RUBIN_OBJECT


@pytest.mark.django_db
def test_resolved_name_echoes_the_record_and_snapshot_epoch():
    meta = current_snapshot()
    tns_object('2026xyz', 20.0, 20.0, type='SN Ia', redshift=0.031)

    result = only_result(resolve_tns(name='SN 2026xyz'))

    assert result['tns_snapshot_epoch'] == meta.last_refresh_epoch.isoformat()
    record = result['tns']
    assert record['name'] == '2026xyz'
    assert record['name_prefix'] == 'SN'
    assert record['ra'] == 20.0 and record['dec'] == 20.0
    assert record['classification'] == 'SN Ia'
    assert record['redshift'] == 0.031
    assert record['url'].endswith('/2026xyz')


@pytest.mark.django_db
def test_unknown_name_on_a_current_snapshot_is_not_found_with_the_epoch():
    meta = current_snapshot()

    result = only_result(lookup_objects(inputs=[tns('2026zzz')]))

    assert result['status'] == InputStatus.TNS_NAME_NOT_FOUND
    assert result['tns_snapshot_epoch'] == meta.last_refresh_epoch.isoformat()
    assert result['tns'] is None
    assert result['objects'] == []


@pytest.mark.django_db
def test_radius_defaults_to_the_tns_match_radius_and_is_overridable():
    current_snapshot()
    tns_object('2026abc', 150.0, 2.0)
    near = alert_at(150.0, 2.0 + 0.5 * ARCSEC)
    far = alert_at(150.0, 2.0 + 3 * ARCSEC)

    with override_settings(TNS_MATCH_RADIUS_ARCSEC=1.0):
        default = only_result(lookup_objects(inputs=[tns('2026abc')]))
    wide = only_result(lookup_objects(inputs=[tns('2026abc', 5.0)]))

    assert default['normalized']['radius_arcsec'] == 1.0
    assert ids_of(default) == [near.lsst_diaObject_diaObjectId]
    assert sorted(ids_of(wide)) == sorted(
        [near.lsst_diaObject_diaObjectId, far.lsst_diaObject_diaObjectId]
    )


@pytest.mark.django_db
@override_settings(API_MAX_CONE_RADIUS_ARCSEC=60)
def test_tns_radius_above_the_cone_maximum_is_rejected():
    with pytest.raises(InvalidQuery) as exc_info:
        resolve_tns(name='2026abc', radius_arcsec=61)
    assert exc_info.value.param == 'radius_arcsec'

    result = lookup_objects(inputs=[tns('2026abc', 61)])['results'][0]
    assert result['status'] == InputStatus.INVALID_INPUT
    assert result['error']['param'] == 'inputs[0].radius_arcsec'


@pytest.mark.django_db
@override_settings(API_MAX_OBJECTS_PER_POSITION=1)
def test_single_tns_lookup_is_capped_like_a_batched_position():
    current_snapshot()
    tns_object('2026abc', 150.0, 2.0)
    alert_at(150.0, 2.0)
    alert_at(150.0, 2.0 + 0.3 * ARCSEC)

    result = only_result(resolve_tns(name='2026abc'))

    assert result['truncated'] is True
    assert result['total'] == 2
    assert len(result['objects']) == 1


# --- snapshot currency (AE5, R31) ---

@pytest.mark.django_db
def test_stale_snapshot_reports_resolver_unavailable_and_ids_resolve():
    # Covers AE5: the refresh task has not run for longer than the max age.
    TnsSnapshotMeta.objects.create(
        pk=1, last_refresh_epoch=timezone.now() - timedelta(days=2)
    )
    tns_object('2026abc', 150.0, 2.0)
    pending = alert_at(150.0, 2.0)
    matched = ObjectCrossmatchRecordFactory().alert
    CatalogMatchFactory(alert=matched)

    body = lookup_objects(inputs=[
        {'kind': 'id', 'diaObjectId': pending.lsst_diaObject_diaObjectId},
        tns('2026abc'),
        {'kind': 'id', 'diaObjectId': matched.lsst_diaObject_diaObjectId},
    ])

    first, by_name, last = body['results']
    assert by_name['status'] == InputStatus.RESOLVER_UNAVAILABLE
    assert by_name['objects'] == []
    assert by_name['tns'] is None
    assert first['status'] == ObjectStatus.CROSSMATCH_PENDING
    assert last['status'] == ObjectStatus.COINCIDENT_SOURCES


@pytest.mark.django_db
def test_absent_snapshot_reports_resolver_unavailable():
    result = only_result(resolve_tns(name='2026abc'))
    assert result['status'] == InputStatus.RESOLVER_UNAVAILABLE


@pytest.mark.django_db
def test_no_fallback_to_stored_tns_associations():
    alert = alert_at(150.0, 2.0)
    TnsAssociation.objects.create(
        alert=alert, checked=True, snapshot_epoch=timezone.now(),
        objid=1, name='2026abc', separation_arcsec=0.1,
    )

    result = only_result(resolve_tns(name='2026abc'))

    assert result['status'] == InputStatus.RESOLVER_UNAVAILABLE


@pytest.mark.django_db
def test_snapshot_currency_helper_is_shared_with_crossmatch():
    now = timezone.now()
    assert TnsSnapshotMeta.current_epoch(now) is None
    meta = TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=now - timedelta(seconds=10))
    with override_settings(TNS_SNAPSHOT_MAX_AGE_SECONDS=60):
        assert TnsSnapshotMeta.current_epoch(now) == meta.last_refresh_epoch
    with override_settings(TNS_SNAPSHOT_MAX_AGE_SECONDS=5):
        assert TnsSnapshotMeta.current_epoch(now) is None


# --- AE6: coincident source is not host association ---

@pytest.mark.django_db
def test_sn_with_offset_host_returns_no_coincident_galaxy(client, openapi_validate):
    # Covers AE6: the SN sits 4 arcsec from its host galaxy's center. The
    # crossmatch radius is 1 arcsec, so the host is not a coincident source.
    current_snapshot()
    tns_object('2026ia', 50.0, -20.0, type='SN Ia')
    sn = ObjectCrossmatchRecordFactory(
        alert=alert_at(50.0, -20.0 + 0.1 * ARCSEC, status=Alert.Status.MATCHED)
    ).alert

    resp = client.get('/api/tns/SN%202026ia', {'detail': 'full'})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('resolve_tns', 200, body)
    result = only_result(body)
    assert result['status'] == InputStatus.OBJECTS_FOUND
    obj = result['objects'][0]
    assert obj['diaObjectId'] == sn.lsst_diaObject_diaObjectId
    assert obj['status'] == ObjectStatus.NO_COINCIDENT_SOURCE
    assert obj['matches'] == []

    doc = client.get('/openapi.json').json()
    statuses = doc['components']['schemas']['ObjectStatus']['description']
    assert 'not a host association' in statuses
    assert 'not evidence of a hostless transient' in statuses.lower()
    tns_result = doc['components']['schemas']['TnsResult']['description']
    assert 'not a host association' in tns_result


# --- AE7: the per-object TNS block (U2, R5, KTD7) ---

@pytest.mark.django_db
def test_ae7_name_lookup_of_object_crossmatched_before_the_name_existed(
    client, openapi_validate,
):
    # Covers AE7: the object was crossmatched while no TNS snapshot was
    # current (a checked=false association row); TNS has since named it. A
    # neighbour within the radius carries a matched association, and a third
    # object predates the TNS feature (no row), so every block shape validates.
    meta = current_snapshot()
    tns_object('2026ae', 60.0, -10.0, type='SN II', redshift=0.02)
    named_later = ObjectCrossmatchRecordFactory(
        alert=alert_at(60.0, -10.0, status=Alert.Status.MATCHED)
    ).alert
    TnsAssociation.objects.create(alert=named_later, checked=False)
    neighbour = ObjectCrossmatchRecordFactory(
        alert=alert_at(60.0, -10.0 + 0.5 * ARCSEC, status=Alert.Status.MATCHED)
    ).alert
    TnsAssociation.objects.create(
        alert=neighbour, checked=True, snapshot_epoch=meta.last_refresh_epoch,
        objid=99, name='2026ae', name_prefix='SN', type='SN II', redshift=0.02,
        separation_arcsec=0.5,
    )
    ObjectCrossmatchRecordFactory(
        alert=alert_at(60.0, -10.0 - 0.5 * ARCSEC, status=Alert.Status.MATCHED)
    )

    resp = client.get('/api/tns/SN%202026ae', {'detail': 'full'})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('resolve_tns', 200, body)
    result = only_result(body)
    assert result['tns']['name'] == '2026ae'
    assert result['tns']['url'].endswith('/2026ae')
    blocks = {obj['diaObjectId']: obj['tns'] for obj in result['objects']}
    assert len(blocks) == 3
    later = blocks[named_later.lsst_diaObject_diaObjectId]
    assert later['checked'] is False
    assert later['snapshot_epoch'] is None and later['name'] is None
    assert blocks[neighbour.lsst_diaObject_diaObjectId]['name'] == '2026ae'
    assert None in blocks.values()


# --- HTTP ---

def test_tns_route_is_slashless():
    assert reverse('resolve-tns', args=['2026abc']) == '/api/tns/2026abc'


@pytest.mark.django_db
def test_get_tns_statuses_validate(client, openapi_validate):
    resp = client.get('/api/tns/2026abc')
    assert resp.status_code == 200
    openapi_validate('resolve_tns', 200, resp.json())
    assert resp.json()['results'][0]['status'] == InputStatus.RESOLVER_UNAVAILABLE
    assert resp.json()['results'][0]['input'] == '2026abc'

    current_snapshot()
    resp = client.get('/api/tns/2026abc')
    openapi_validate('resolve_tns', 200, resp.json())
    assert resp.json()['results'][0]['status'] == InputStatus.TNS_NAME_NOT_FOUND


@pytest.mark.parametrize('path, params, param', [
    ('/api/tns/2026abc', {'radius_arcsec': '61'}, 'radius_arcsec'),
    ('/api/tns/2026abc', {'radius_arcsec': 'wide'}, 'radius_arcsec'),
    ('/api/tns/2026abc', {'detail': 'bogus'}, 'detail'),
    ('/api/tns/SN', {}, 'name'),
])
@pytest.mark.django_db
@override_settings(API_MAX_CONE_RADIUS_ARCSEC=60)
def test_get_tns_bad_parameter_is_400(client, openapi_validate, path, params, param):
    resp = client.get(path, params)
    assert resp.status_code == 400
    openapi_validate('resolve_tns', 400, resp.json())
    assert resp.json()['param'] == param


@pytest.mark.django_db
def test_mixed_batch_with_malformed_tns_and_position_validates(client, openapi_validate):
    # A malformed TNS entry and a malformed position fail only themselves.
    current_snapshot()
    tns_object('2026abc', 150.0, 2.0)
    alert_at(150.0, 2.0)

    resp = client.post(LOOKUP, data=json.dumps({
        'inputs': [
            {'kind': 'tns', 'name': 42},
            {'kind': 'tns', 'name': 'SN 2026abc'},
            {'kind': 'position', 'ra': 150.0},
            {'kind': 'position', 'ra': 150.0, 'dec': 2.0, 'radius_arcsec': 1.0},
            {'kind': 'tns', 'name': '2026nope'},
        ],
    }), content_type='application/json')

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('lookup_objects', 200, body)
    statuses = [r['status'] for r in body['results']]
    assert statuses == [
        InputStatus.INVALID_INPUT, InputStatus.OBJECTS_FOUND,
        InputStatus.INVALID_INPUT, InputStatus.OBJECTS_FOUND,
        InputStatus.TNS_NAME_NOT_FOUND,
    ]
    assert body['results'][0]['error']['param'] == 'inputs[0].name'
    assert body['results'][2]['error']['param'] == 'inputs[2].dec'


@pytest.mark.django_db
def test_stale_snapshot_batch_validates(client, openapi_validate):
    TnsSnapshotMeta.objects.create(
        pk=1, last_refresh_epoch=timezone.now() - timedelta(days=2)
    )
    resp = client.post(LOOKUP, data=json.dumps({
        'inputs': [{'kind': 'id', 'diaObjectId': 1}, tns('2026abc'),
                   {'kind': 'id', 'diaObjectId': 2}],
    }), content_type='application/json')

    assert resp.status_code == 200
    openapi_validate('lookup_objects', 200, resp.json())
