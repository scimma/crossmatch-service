"""U6: filters on the new queries (R13, R14; KTD9).

Catalog-qualified ``<catalog>.<column>_min|_max`` filters and the generic
``catalog``, ``separation_arcsec_max``, and ``reliability_min|_max`` filters
on ``POST api/lookup`` (a ``filters`` body object), ``GET api/objects/<id>``,
``GET api/cone``, and ``GET api/tns/<name>`` (query parameters). Every entry is
still returned, marked ``qualifies`` with a ``qualifies_reason``; every
response validates against the served OpenAPI document. Also covers the
``unsupported_parameter`` 400 of ``recent-crossmatches`` (KTD9 step 6).
"""

import json

import pytest
from django.utils import timezone

from api.contract import ErrorCode, InputStatus, ObjectStatus, QualifiesReason
from api.filters import filter_parameters
from core.healpix import radec_to_ipix
from core.models import Alert, TnsObject, TnsSnapshotMeta
from tests.factories import (
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

LOOKUP = '/api/lookup'
CONE = '/api/cone'
RECENT = '/api/recent-crossmatches'
ARCSEC = 1.0 / 3600.0
UNKNOWN_ID = 4_242_424_242
POE_MIN = 'gaia_dr3.parallax_over_error_min'


def _post(client, body):
    return client.post(LOOKUP, data=json.dumps(body), content_type='application/json')


def _id(alert_or_id):
    oid = getattr(alert_or_id, 'lsst_diaObject_diaObjectId', alert_or_id)
    return {'kind': 'id', 'diaObjectId': oid}


def _alert(ra=180.0, dec=-30.0, **kwargs):
    return AlertFactory(ra_deg=ra, dec_deg=dec, healpix_ipix=radec_to_ipix(ra, dec), **kwargs)


def matched(catalog='gaia_dr3', payload=None, ra=180.0, dec=-30.0, **alert_kwargs):
    """A crossmatched object with one match in ``catalog`` carrying ``payload``."""
    record = ObjectCrossmatchRecordFactory(
        alert=_alert(ra, dec, status=Alert.Status.MATCHED, **alert_kwargs)
    )
    CatalogMatchFactory(
        alert=record.alert, catalog_name=catalog, catalog_payload=payload or {},
    )
    return record.alert


def no_match(**alert_kwargs):
    """A crossmatched object with no coincident source."""
    return ObjectCrossmatchRecordFactory(
        alert=_alert(status=Alert.Status.MATCHED, **alert_kwargs)
    ).alert


def gaia(parallax, parallax_error=1.0, **extra):
    return {'parallax': parallax, 'parallax_error': parallax_error, **extra}


def results_of(resp):
    return resp.json()['results']


def marks(result):
    return result['qualifies'], result['qualifies_reason']


# --- the filter registry (R13) ---


def test_documented_filter_set_matches_r13():
    names = {param.name for param in filter_parameters()}
    for column in (
        'parallax', 'parallax_error', 'parallax_over_error', 'pmra', 'pmdec', 'ruwe',
        'classprob_dsc_combmod_star', 'classprob_dsc_combmod_galaxy',
        'classprob_dsc_combmod_quasar',
    ):
        assert f'gaia_dr3.{column}_min' in names
        assert f'gaia_dr3.{column}_max' in names
    for catalog in ('des_y6_gold', 'delve_dr3_gold'):
        for column in ('dnf_z', 'dnf_zsigma', 'ext_mash'):
            assert f'{catalog}.{column}_min' in names
            assert f'{catalog}.{column}_max' in names
    assert {'catalog', 'separation_arcsec_max', 'reliability_min', 'reliability_max'} <= names
    assert not any(name.startswith('skymapper_dr4.') for name in names)


def test_every_filter_parameter_has_a_unit_and_description():
    for param in filter_parameters():
        assert param.description.strip(), param.name
        if param.field is not None:
            assert param.field.unit.strip(), param.name


def test_every_qualifies_reason_is_described(client):
    from api.contract import CODE_DESCRIPTIONS

    for value in QualifiesReason.values:
        assert CODE_DESCRIPTIONS[value].strip(), value
    schema = client.get('/openapi.json').json()['components']['schemas']['QualifiesReason']
    assert schema['enum'] == QualifiesReason.values


# --- qualification over ID inputs (R14, KTD9 step 4) ---


@pytest.mark.django_db
def test_filtered_batch_returns_every_input_marked_with_a_reason(client, openapi_validate):
    significant = matched(payload=gaia(12.0, 2.0))       # 6.0
    insignificant = matched(payload=gaia(3.0, 1.0))      # 3.0
    des_only = matched(catalog='des_y6_gold', payload={'dnf_z': 0.3})
    nothing = no_match()
    pending = _alert()
    body = {
        'inputs': [
            _id(significant), _id(insignificant), _id(des_only), _id(nothing),
            _id(pending), _id(UNKNOWN_ID), {'kind': 'id', 'diaObjectId': 'x'},
        ],
        'filters': {POE_MIN: 5},
    }

    resp = _post(client, body)

    assert resp.status_code == 200
    openapi_validate('lookup_objects', 200, resp.json())
    results = results_of(resp)
    assert len(results) == 7
    assert [marks(r) for r in results] == [
        (True, QualifiesReason.MEETS_FILTERS),
        (False, QualifiesReason.NO_SOURCE_MEETS_FILTERS),
        (False, QualifiesReason.NO_SOURCE_MEETS_FILTERS),
        (False, QualifiesReason.NO_COINCIDENT_SOURCE),
        (False, QualifiesReason.CROSSMATCH_PENDING),
        (False, QualifiesReason.NOT_IN_SERVICE),
        (False, QualifiesReason.INVALID_INPUT),
    ]
    for result in results[:6]:
        (obj,) = result['objects']
        assert marks(obj) == marks(result)
    assert resp.json()['filters'] == {POE_MIN: 5.0}


@pytest.mark.django_db
def test_unfiltered_responses_carry_no_qualification(client):
    alert = matched(payload=gaia(12.0, 2.0))

    body = _post(client, {'inputs': [_id(alert)]}).json()

    assert 'filters' not in body
    assert 'qualifies' not in body['results'][0]
    assert 'qualifies' not in body['results'][0]['objects'][0]


@pytest.mark.django_db
def test_bounds_are_inclusive(client):
    alert = matched(payload=gaia(5.0, 1.0))

    body = {'inputs': [_id(alert)], 'filters': {POE_MIN: 5, 'gaia_dr3.parallax_over_error_max': 5}}

    assert results_of(_post(client, body))[0]['qualifies'] is True


@pytest.mark.parametrize('payload', [
    {'parallax': None, 'parallax_error': 1.0},
    {'parallax': 'NaN', 'parallax_error': 1.0},
    {'parallax': '12.0', 'parallax_error': 1.0},
    {'parallax': 12.0, 'parallax_error': None},
    {'parallax': 12.0, 'parallax_error': 'abc'},
    {'parallax': 12.0, 'parallax_error': 0},
    {'parallax': [1, 2], 'parallax_error': 1.0},
    {},
])
@pytest.mark.django_db
def test_null_or_non_numeric_values_never_qualify_and_never_raise(client, openapi_validate, payload):
    alert = matched(payload=payload)
    body = {
        'inputs': [_id(alert)],
        'filters': {POE_MIN: -1000, 'gaia_dr3.parallax_min': -1000},
    }

    resp = _post(client, body)

    assert resp.status_code == 200
    openapi_validate('lookup_objects', 200, resp.json())
    assert marks(results_of(resp)[0]) == (False, QualifiesReason.NO_SOURCE_MEETS_FILTERS)


@pytest.mark.django_db
def test_null_catalog_payload_never_qualifies(client):
    record = ObjectCrossmatchRecordFactory(alert=_alert(status=Alert.Status.MATCHED))
    CatalogMatchFactory(alert=record.alert, catalog_payload=None)

    body = {'inputs': [_id(record.alert)], 'filters': {'gaia_dr3.ruwe_max': 100}}

    assert results_of(_post(client, body))[0]['qualifies'] is False


@pytest.mark.django_db
def test_negative_parallax_gives_negative_significance(client):
    alert = matched(payload=gaia(-12.0, 2.0))  # -6.0

    positive = {'inputs': [_id(alert)], 'filters': {POE_MIN: 5}}
    negative = {'inputs': [_id(alert)], 'filters': {'gaia_dr3.parallax_over_error_max': -5}}

    assert results_of(_post(client, positive))[0]['qualifies'] is False
    assert results_of(_post(client, negative))[0]['qualifies'] is True


@pytest.mark.django_db
def test_one_source_must_satisfy_all_match_filters(client):
    """Two Gaia sources each satisfy one filter; neither satisfies both."""
    record = ObjectCrossmatchRecordFactory(alert=_alert(status=Alert.Status.MATCHED))
    CatalogMatchFactory(alert=record.alert, catalog_payload={'ruwe': 3.0, 'pmra': 1.0})
    CatalogMatchFactory(alert=record.alert, catalog_payload={'ruwe': 1.0, 'pmra': 50.0})

    both = {'inputs': [_id(record.alert)],
            'filters': {'gaia_dr3.ruwe_min': 2, 'gaia_dr3.pmra_min': 10}}
    one = {'inputs': [_id(record.alert)], 'filters': {'gaia_dr3.ruwe_min': 2}}

    assert results_of(_post(client, both))[0]['qualifies'] is False
    assert results_of(_post(client, one))[0]['qualifies'] is True


@pytest.mark.django_db
def test_uppercase_catalog_columns_filter_on_lowercased_payload_keys(client):
    """DES columns are UPPERCASE in settings; stored payload keys are lowercased."""
    alert = matched(catalog='des_y6_gold', payload={'dnf_z': 0.3, 'ext_mash': 4})

    body = {'inputs': [_id(alert)],
            'filters': {'des_y6_gold.dnf_z_max': 0.5, 'des_y6_gold.ext_mash_min': 3}}

    assert results_of(_post(client, body))[0]['qualifies'] is True


@pytest.mark.django_db
def test_only_the_current_match_version_counts(client):
    record = ObjectCrossmatchRecordFactory(
        alert=_alert(status=Alert.Status.MATCHED), match_version=2,
    )
    CatalogMatchFactory(alert=record.alert, catalog_source_id='s1', match_version=1,
                        catalog_payload=gaia(12.0, 1.0))
    CatalogMatchFactory(alert=record.alert, catalog_source_id='s1', match_version=2,
                        catalog_payload=gaia(1.0, 1.0))

    body = {'inputs': [_id(record.alert)], 'filters': {POE_MIN: 5}}

    assert results_of(_post(client, body))[0]['qualifies'] is False


@pytest.mark.django_db
def test_matches_on_a_pending_object_never_qualify(client):
    """KTD9 step 5: partial rows of a reverted batch never surface."""
    alert = _alert(status=Alert.Status.QUEUED)
    CatalogMatchFactory(alert=alert, catalog_payload=gaia(12.0, 1.0))

    body = {'inputs': [_id(alert)], 'filters': {POE_MIN: 5}}

    assert marks(results_of(_post(client, body))[0]) == (
        False, QualifiesReason.CROSSMATCH_PENDING,
    )


# --- generic filters ---


@pytest.mark.django_db
def test_catalog_filter_requires_a_source_in_that_catalog(client):
    gaia_alert = matched(payload=gaia(1.0))
    des_alert = matched(catalog='des_y6_gold')

    body = {'inputs': [_id(gaia_alert), _id(des_alert)], 'filters': {'catalog': 'des_y6_gold'}}

    assert [r['qualifies'] for r in results_of(_post(client, body))] == [False, True]


@pytest.mark.django_db
def test_separation_filter(client):
    alert = matched(payload=gaia(1.0))  # the factory separation is 0.5 arcsec

    near = {'inputs': [_id(alert)], 'filters': {'separation_arcsec_max': 0.5}}
    far = {'inputs': [_id(alert)], 'filters': {'separation_arcsec_max': 0.4}}

    assert results_of(_post(client, near))[0]['qualifies'] is True
    assert results_of(_post(client, far))[0]['qualifies'] is False


@pytest.mark.django_db
def test_reliability_is_an_object_filter(client):
    reliable = _alert(reliability=0.9)
    unreliable = matched(payload=gaia(12.0, 1.0), reliability=0.2)
    unknown = _alert(reliability=None)

    body = {
        'inputs': [_id(reliable), _id(unreliable), _id(unknown)],
        'filters': {'reliability_min': 0.5},
    }
    results = results_of(_post(client, body))

    # A reliability filter alone qualifies a pending object on its reliability.
    assert marks(results[0]) == (True, QualifiesReason.MEETS_FILTERS)
    assert marks(results[1]) == (False, QualifiesReason.OBJECT_FILTERS_NOT_MET)
    assert marks(results[2]) == (False, QualifiesReason.OBJECT_FILTERS_NOT_MET)


# --- request-level errors ---


@pytest.mark.django_db
def test_filters_spanning_catalogs_are_rejected_naming_both(client, openapi_validate):
    alert = matched(payload=gaia(12.0))
    body = {
        'inputs': [_id(alert)],
        'filters': {POE_MIN: 5, 'des_y6_gold.dnf_z_max': 0.5},
    }

    resp = _post(client, body)

    assert resp.status_code == 400
    error = resp.json()
    openapi_validate('lookup_objects', 400, error)
    assert error['code'] == ErrorCode.FILTERS_SPAN_CATALOGS
    assert sorted(error['params']) == sorted([POE_MIN, 'des_y6_gold.dnf_z_max'])


@pytest.mark.django_db
def test_catalog_filter_conflicting_with_a_column_filter_spans_catalogs(client):
    body = {'inputs': [_id(1)], 'filters': {'catalog': 'des_y6_gold', POE_MIN: 5}}

    error = _post(client, body).json()

    assert error['code'] == ErrorCode.FILTERS_SPAN_CATALOGS
    assert sorted(error['params']) == sorted(['catalog', POE_MIN])


@pytest.mark.parametrize('name', [
    'gaia_dr3.not_a_column_min',
    'gaia_dr3.phot_g_mean_mag_min',   # a payload column, not a filter column
    'nope_dr1.parallax_min',
    'gaia_dr3.parallax',
    'parallax_min',
])
@pytest.mark.django_db
def test_unknown_filter_name_is_a_400_naming_it(client, openapi_validate, name):
    resp = _post(client, {'inputs': [_id(1)], 'filters': {name: 1}})

    assert resp.status_code == 400
    openapi_validate('lookup_objects', 400, resp.json())
    assert resp.json()['code'] == ErrorCode.INVALID_PARAMETER
    assert resp.json()['param'] == name


@pytest.mark.parametrize('filters, param', [
    ({POE_MIN: 'five'}, POE_MIN),
    ({POE_MIN: '5'}, POE_MIN),       # the JSON body takes numbers
    ({POE_MIN: True}, POE_MIN),
    ({'catalog': 'nope'}, 'catalog'),
    ({'catalog': 3}, 'catalog'),
    ({'separation_arcsec_max': -1}, 'separation_arcsec_max'),
])
@pytest.mark.django_db
def test_invalid_filter_values_are_a_400_naming_them(client, filters, param):
    resp = _post(client, {'inputs': [_id(1)], 'filters': filters})

    assert resp.status_code == 400
    assert resp.json()['param'] == param


@pytest.mark.django_db
def test_filters_must_be_an_object(client):
    resp = _post(client, {'inputs': [_id(1)], 'filters': [POE_MIN]})

    assert resp.status_code == 400
    assert resp.json()['param'] == 'filters'


# --- the same filters on GET object, cone, and TNS ---


@pytest.mark.django_db
def test_get_object_accepts_filter_query_params(client, openapi_validate):
    alert = matched(payload=gaia(12.0, 2.0))

    resp = client.get(f'/api/objects/{alert.lsst_diaObject_diaObjectId}', {POE_MIN: '5'})

    assert resp.status_code == 200
    openapi_validate('get_object', 200, resp.json())
    assert marks(results_of(resp)[0]) == (True, QualifiesReason.MEETS_FILTERS)


@pytest.mark.django_db
def test_cone_marks_objects_and_counts_qualifying_within_the_radius(client, openapi_validate):
    good = matched(payload=gaia(12.0, 2.0), ra=150.0, dec=2.0)
    matched(payload=gaia(1.0, 2.0), ra=150.0, dec=2.0 + ARCSEC)
    _alert(150.0, 2.0 - ARCSEC)

    resp = client.get(CONE, {'ra': '150', 'dec': '2', 'radius_arcsec': '5', POE_MIN: '5'})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('cone_search', 200, body)
    (result,) = body['results']
    assert result['total'] == 3
    assert result['qualifying_total'] == 1
    assert marks(result) == (True, QualifiesReason.MEETS_FILTERS)
    by_id = {obj['diaObjectId']: marks(obj) for obj in result['objects']}
    assert len(by_id) == 3  # nothing is dropped
    assert by_id[good.lsst_diaObject_diaObjectId] == (True, QualifiesReason.MEETS_FILTERS)
    assert sorted(reason for _q, reason in by_id.values()) == sorted([
        QualifiesReason.MEETS_FILTERS, QualifiesReason.NO_SOURCE_MEETS_FILTERS,
        QualifiesReason.CROSSMATCH_PENDING,
    ])
    assert body['filters'] == {POE_MIN: 5.0}


@pytest.mark.django_db
def test_cone_qualifying_total_covers_objects_past_the_page(client, openapi_validate):
    for step in range(3):
        matched(payload=gaia(12.0, 1.0), ra=150.0, dec=2.0 + step * ARCSEC)

    resp = client.get(CONE, {
        'ra': '150', 'dec': '2', 'radius_arcsec': '5', 'page_size': '1', POE_MIN: '5',
    })

    body = resp.json()
    openapi_validate('cone_search', 200, body)
    assert len(body['results'][0]['objects']) == 1
    assert body['results'][0]['qualifying_total'] == 3
    # Filters are not pinned by the cursor; the listed set does not depend on them.
    nxt = client.get(CONE, {'cursor': body['next_cursor'], POE_MIN: '5'})
    assert nxt.status_code == 200
    openapi_validate('cone_search', 200, nxt.json())
    assert nxt.json()['results'][0]['objects'][0]['qualifies'] is True


@pytest.mark.django_db
def test_empty_cone_does_not_qualify(client, openapi_validate):
    resp = client.get(CONE, {'ra': '10', 'dec': '10', 'radius_arcsec': '5', POE_MIN: '5'})

    body = resp.json()
    openapi_validate('cone_search', 200, body)
    result = body['results'][0]
    assert result['status'] == InputStatus.NO_RUBIN_OBJECT
    assert result['qualifying_total'] == 0
    assert marks(result) == (False, QualifiesReason.NO_RUBIN_OBJECT)


@pytest.mark.django_db
def test_cone_rejects_unknown_filter_and_spanning_filters(client, openapi_validate):
    base = {'ra': '150', 'dec': '2', 'radius_arcsec': '5'}

    unknown = client.get(CONE, {**base, 'gaia_dr3.bogus_min': '1'})
    span = client.get(CONE, {**base, POE_MIN: '5', 'delve_dr3_gold.dnf_z_max': '1'})

    assert unknown.status_code == 400
    openapi_validate('cone_search', 400, unknown.json())
    assert unknown.json()['param'] == 'gaia_dr3.bogus_min'
    assert span.status_code == 400
    openapi_validate('cone_search', 400, span.json())
    assert span.json()['code'] == ErrorCode.FILTERS_SPAN_CATALOGS


@pytest.mark.django_db
def test_batched_positions_and_tns_are_marked(client, openapi_validate):
    good = matched(payload=gaia(12.0, 2.0), ra=150.0, dec=2.0)
    TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())
    TnsObject.objects.create(
        objid=1, name='2026abc', name_prefix='SN', ra_deg=150.0, dec_deg=2.0,
        type='SN Ia', redshift=0.05, healpix_ipix=radec_to_ipix(150.0, 2.0),
    )
    body = {
        'inputs': [
            {'kind': 'position', 'ra': 150.0, 'dec': 2.0, 'radius_arcsec': 2},
            {'kind': 'position', 'ra': 10.0, 'dec': 10.0, 'radius_arcsec': 2},
            {'kind': 'tns', 'name': 'SN 2026abc', 'radius_arcsec': 2},
            {'kind': 'tns', 'name': '2026zzz'},
            _id(good),
        ],
        'filters': {POE_MIN: 5},
    }

    resp = _post(client, body)

    assert resp.status_code == 200
    openapi_validate('lookup_objects', 200, resp.json())
    results = results_of(resp)
    assert [marks(r) for r in results] == [
        (True, QualifiesReason.MEETS_FILTERS),
        (False, QualifiesReason.NO_RUBIN_OBJECT),
        (True, QualifiesReason.MEETS_FILTERS),
        (False, QualifiesReason.TNS_NAME_NOT_FOUND),
        (True, QualifiesReason.MEETS_FILTERS),
    ]
    assert [r.get('qualifying_total') for r in results[:4]] == [1, 0, 1, None]


@pytest.mark.django_db
def test_tns_get_accepts_filter_query_params(client, openapi_validate):
    matched(payload=gaia(1.0, 2.0), ra=150.0, dec=2.0)
    TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())
    TnsObject.objects.create(
        objid=1, name='2026abc', name_prefix='SN', ra_deg=150.0, dec_deg=2.0,
        type='SN Ia', redshift=0.05, healpix_ipix=radec_to_ipix(150.0, 2.0),
    )

    resp = client.get('/api/tns/2026abc', {'radius_arcsec': '2', POE_MIN: '5'})

    assert resp.status_code == 200
    openapi_validate('resolve_tns', 200, resp.json())
    result = results_of(resp)[0]
    assert marks(result) == (False, QualifiesReason.NO_OBJECT_MEETS_FILTERS)
    assert result['qualifying_total'] == 0
    assert marks(result['objects'][0]) == (False, QualifiesReason.NO_SOURCE_MEETS_FILTERS)


@pytest.mark.django_db
def test_tns_get_rejects_unknown_filter(client, openapi_validate):
    resp = client.get('/api/tns/2026abc', {'gaia_dr3.nope_max': '1'})

    assert resp.status_code == 400
    openapi_validate('resolve_tns', 400, resp.json())
    assert resp.json()['param'] == 'gaia_dr3.nope_max'


# --- recent-crossmatches does not filter (KTD9 step 6) ---


@pytest.mark.parametrize('params, offending', [
    ({POE_MIN: '5'}, POE_MIN),
    ({'des_y6_gold.dnf_z_max': '0.5'}, 'des_y6_gold.dnf_z_max'),
    ({'gaia_dr3.not_a_column_min': '1'}, 'gaia_dr3.not_a_column_min'),
    ({'catalog': 'gaia_dr3'}, 'catalog'),
    ({'separation_arcsec_max': '1'}, 'separation_arcsec_max'),
    ({'reliability_min': '0.5'}, 'reliability_min'),
    ({'response': 'count'}, 'response'),
])
@pytest.mark.django_db
def test_recent_crossmatches_rejects_filter_and_response_params(
    client, openapi_validate, params, offending,
):
    CatalogMatchFactory(alert=AlertFactory())

    resp = client.get(RECENT, params)

    assert resp.status_code == 400
    error = resp.json()
    openapi_validate('recent_crossmatches', 400, error)
    assert error['code'] == ErrorCode.UNSUPPORTED_PARAMETER
    assert error['param'] == offending
    assert error['error'] == error['message']


@pytest.mark.django_db
def test_recent_crossmatches_names_every_unsupported_param(client, openapi_validate):
    resp = client.get(RECENT, {POE_MIN: '5', 'response': 'count'})

    assert resp.status_code == 400
    openapi_validate('recent_crossmatches', 400, resp.json())
    assert sorted(resp.json()['params']) == sorted([POE_MIN, 'response'])


@pytest.mark.django_db
def test_recent_crossmatches_still_ignores_other_unknown_params(client, openapi_validate):
    CatalogMatchFactory(alert=AlertFactory())

    resp = client.get(RECENT, {'foo': 'bar', 'radius_arcsec': '5', 'catalogue': 'x'})

    assert resp.status_code == 200
    openapi_validate('recent_crossmatches', 200, resp.json())
    assert resp.json()['count'] == 1
