"""U5: the window summary HTTP view, ``GET api/summary`` (R17, R19; KTD1, KTD4).

Routing, the method check, the unsupported-parameter guard, 400s naming the
parameter, and conformance of every response to the served OpenAPI document
(the ``openapi_validate`` fixture). The statistics themselves are covered by
``test_summary_service``. The view reads one REPEATABLE READ snapshot, which
needs a real transaction per request, so database tests use
``transaction=True``.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from api.contract import ErrorCode
from api.summary import default_night, night_start
from core.models import Alert
from tests.factories import (
    AlertDeliveryFactory,
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

SUMMARY = '/api/summary'

pytestmark = pytest.mark.django_db(transaction=True)


def _past_night():
    """A night that has begun and ended, whatever the clock says."""
    return default_night(timezone.now()) - timedelta(days=2)


def test_summary_route_is_slashless():
    assert reverse('summary') == SUMMARY


def test_night_summary_validates(client, openapi_validate):
    night = _past_night()
    observed = night_start(night) + timedelta(hours=6)
    matched = AlertFactory(
        event_time=observed,
        event_time_basis=Alert.EventTimeBasis.FIRST_DETECTION,
        status=Alert.Status.MATCHED,
        reliability=0.8,
    )
    AlertDeliveryFactory(alert=matched, broker='lasair')
    AlertDeliveryFactory(alert=matched, broker='antares')
    ObjectCrossmatchRecordFactory(alert=matched)
    CatalogMatchFactory(alert=matched, catalog_name='gaia_dr3')
    AlertFactory(event_time=observed, reliability=None)  # pending, no basis

    resp = client.get(SUMMARY, {'night': night.isoformat(), 'top_n': '3'})

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 200, body)
    assert body['window']['nights'] == [night.isoformat()]
    assert body['totals']['objects'] == 2
    assert body['top'][0]['diaObjectId_str'] == str(matched.lsst_diaObject_diaObjectId)
    assert body['request'].startswith(SUMMARY + '?')


def test_without_catalog_filter_validates(client, openapi_validate):
    night = _past_night()
    resp = client.get(
        SUMMARY,
        {
            'night': night.isoformat(),
            'catalog': 'gaia_dr3',
            'catalog_match': 'without',
            'broker': 'Pitt-Google',
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 200, body)
    assert 'coincident_sources' not in body['catalogs']['by_catalog']['gaia_dr3']
    assert body['filters'] == {
        'broker': 'pittgoogle',
        'catalog': 'gaia_dr3',
        'catalog_match': 'without',
    }


def test_no_parameters_summarize_the_default_night(client, openapi_validate):
    before = default_night(timezone.now())
    resp = client.get(SUMMARY)
    after = default_night(timezone.now())

    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 200, body)
    assert body['window']['form'] == 'default'
    assert body['window']['nights'][0] in {before.isoformat(), after.isoformat()}
    assert body['totals']['objects'] == 0
    assert 'empty_window' in body['notes']


def test_post_is_405(client, openapi_validate):
    resp = client.post(SUMMARY)

    assert resp.status_code == 405
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 405, body)
    assert body['code'] == ErrorCode.METHOD_NOT_ALLOWED


@pytest.mark.parametrize(
    'query, param',
    [
        ({'night': 'yesterday'}, 'night'),
        ({'last_nights': '0'}, 'last_nights'),
        ({'top_n': 'many'}, 'top_n'),
        ({'broker': 'ztf'}, 'broker'),
        ({'catalog_match': 'with'}, 'catalog_match'),
    ],
)
def test_invalid_parameter_is_a_400_naming_it(client, openapi_validate, query, param):
    resp = client.get(SUMMARY, query)

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 400, body)
    assert body['code'] == ErrorCode.INVALID_PARAMETER
    assert body['param'] == param


def test_two_catalogs_is_filters_span_catalogs(client, openapi_validate):
    resp = client.get(SUMMARY + '?catalog=gaia_dr3&catalog=des_y6_gold')

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 400, body)
    assert body['code'] == ErrorCode.FILTERS_SPAN_CATALOGS
    assert body['params'] == ['catalog', 'catalog']


def test_unsupported_parameter_is_a_400(client, openapi_validate):
    resp = client.get(SUMMARY, {'night': _past_night().isoformat(), 'detail': 'full'})

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 400, body)
    assert body['code'] == ErrorCode.UNSUPPORTED_PARAMETER
    assert body['param'] == 'detail'


def test_several_unsupported_parameters_are_all_named(client, openapi_validate):
    resp = client.get(SUMMARY, {'ra': '1', 'gaia_dr3.phot_g_mean_mag_max': '18'})

    assert resp.status_code == 400
    body = resp.json()
    openapi_validate('summarize_rubin_transients_by_night', 400, body)
    assert body['code'] == ErrorCode.UNSUPPORTED_PARAMETER
    assert sorted(body['params']) == ['gaia_dr3.phot_g_mean_mag_max', 'ra']
