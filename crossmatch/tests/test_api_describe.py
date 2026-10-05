"""U7 / R22, R23; KTD16: ``GET api/describe`` and the footprint-order setting.

The describe endpoint gives agents, at runtime, the vocabulary and limits they
must respect: the catalogs in service (release, what ``catalog_source_id``
means, filterable fields with units, coverage-map resolution), the per-request
maximums and budget, detail levels, response modes, generic filters, the
crossmatch and TNS radii, the broker cut table, and the provenance recording
release. Everything is read from live settings; the web tier never opens a
LSDB/HATS catalog to answer it.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse

from api.contract import CODE_DESCRIPTIONS
from api.filters import GENERIC_FILTERS, RESPONSE_MODES, filter_fields
from api.service import DEFAULT_DETAIL, DETAIL_LEVELS
from core import provenance
from project.settings import _validate_footprint_moc_orders

DESCRIBE = '/api/describe'
APP_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ORDERS = {
    'gaia_dr3': 8,
    'des_y6_gold': 6,
    'delve_dr3_gold': 10,
    'skymapper_dr4': 8,
}


def _catalog(body, name):
    return next(c for c in body['catalogs'] if c['name'] == name)


def test_describe_route_is_slashless():
    assert reverse('describe-service') == DESCRIBE


def test_describe_lists_every_configured_catalog(client, openapi_validate):
    resp = client.get(DESCRIBE)

    assert resp.status_code == 200
    body = resp.json()
    assert [c['name'] for c in body['catalogs']] == [
        c['name'] for c in settings.CROSSMATCH_CATALOGS
    ]
    for cfg in settings.CROSSMATCH_CATALOGS:
        entry = _catalog(body, cfg['name'])
        assert entry['release'] == cfg['release']
        assert entry['catalog_source_id']['column'] == cfg['source_id_column']
        assert entry['catalog_source_id']['description'].strip()
        coverage = entry['coverage_map']
        assert coverage['footprint_moc_order'] == DEFAULT_ORDERS[cfg['name']]
        assert 'searched' in coverage['note']
    openapi_validate('describe_service', 200, body)


def test_source_id_meaning_names_each_catalog_column(client):
    body = client.get(DESCRIBE).json()

    assert 'source_id' in _catalog(body, 'gaia_dr3')['catalog_source_id']['description']
    for name in ('des_y6_gold', 'delve_dr3_gold'):
        assert 'COADD_OBJECT_ID' in _catalog(body, name)['catalog_source_id']['description']
    assert 'object_id' in _catalog(body, 'skymapper_dr4')['catalog_source_id']['description']


def test_coverage_resolution_matches_the_healpix_order(client):
    body = client.get(DESCRIBE).json()

    resolutions = {
        c['name']: c['coverage_map']['resolution_arcmin'] for c in body['catalogs']
    }
    assert resolutions['gaia_dr3'] == pytest.approx(13.7, abs=0.1)
    assert resolutions['des_y6_gold'] == pytest.approx(55.0, abs=0.1)
    assert resolutions['delve_dr3_gold'] == pytest.approx(3.4, abs=0.1)
    assert resolutions['skymapper_dr4'] == pytest.approx(13.7, abs=0.1)


def test_filterable_fields_carry_units_and_parameter_names(client):
    body = client.get(DESCRIBE).json()

    for field in filter_fields():
        entry = _catalog(body, field.catalog)
        described = next(f for f in entry['filterable_fields'] if f['name'] == field.name)
        assert described['unit'] == field.unit
        assert described['parameters'] == [
            f'{field.catalog}.{field.name}_min', f'{field.catalog}.{field.name}_max',
        ]
    gaia = _catalog(body, 'gaia_dr3')
    derived = next(
        f for f in gaia['filterable_fields'] if f['name'] == 'parallax_over_error'
    )
    assert derived['column'] is None
    assert derived['derived_from'] == {
        'numerator': 'parallax', 'denominator': 'parallax_error',
    }
    assert _catalog(body, 'skymapper_dr4')['filterable_fields'] == []


@override_settings(
    API_REQUEST_BUDGET_SECONDS=2.5,
    API_MAX_IDS=17,
    API_MAX_POSITIONS=11,
    API_MAX_CONE_RADIUS_ARCSEC=12.5,
    API_MAX_OBJECTS_PER_POSITION=7,
    API_MAX_OBJECTS_PER_REQUEST=99,
    RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE=21,
    RECENT_CROSSMATCH_MAX_PAGE_SIZE=210,
    RECENT_CROSSMATCH_MAX_WINDOW_HOURS=5,
    CROSSMATCH_RADIUS_ARCSEC=1.5,
    TNS_MATCH_RADIUS_ARCSEC=3.0,
)
def test_limits_and_radii_follow_live_settings(client, openapi_validate):
    body = client.get(DESCRIBE).json()

    assert body['limits'] == {
        'request_budget_seconds': 2.5,
        'max_ids': 17,
        'max_positions': 11,
        'max_cone_radius_arcsec': 12.5,
        'max_objects_per_position': 7,
        'max_objects_per_request': 99,
        'recent_crossmatches': {
            'default_page_size': 21,
            'max_page_size': 210,
            'max_window_hours': 5,
        },
    }
    assert body['crossmatch']['radius_arcsec'] == 1.5
    assert body['crossmatch']['nearest_source_per_catalog'] is True
    assert 'nearest' in body['crossmatch']['description']
    assert body['tns'] == {'default_radius_arcsec': 3.0, 'max_radius_arcsec': 12.5}
    assert body['provenance']['crossmatch_radius_arcsec'] == 1.5
    openapi_validate('describe_service', 200, body)


def test_vocabulary_and_cut_table(client):
    body = client.get(DESCRIBE).json()

    assert [d['name'] for d in body['detail_levels']] == list(DETAIL_LEVELS)
    assert body['default_detail'] == DEFAULT_DETAIL
    assert [m['name'] for m in body['response_modes']] == list(RESPONSE_MODES)
    assert [f['name'] for f in body['generic_filters']] == list(GENERIC_FILTERS)
    separation = next(
        f for f in body['generic_filters'] if f['name'] == 'separation_arcsec_max'
    )
    assert separation['unit'] == 'arcsec'
    assert separation['is_match_filter'] is True
    assert body['reliability_cuts'] == provenance.reliability_cuts()
    assert body['provenance_recording_release'] == provenance.PROVENANCE_RECORDING_RELEASE


def test_catalog_added_by_override_is_described(client, openapi_validate):
    extra = {
        'name': 'test_cat',
        'hats_url': 's3://nowhere',
        'release': 'Test R1',
        'source_id_column': 'objid',
        'ra_column': 'ra',
        'dec_column': 'dec',
        'payload_columns': ['ra', 'dec', 'mag'],
        'filter_columns': {'mag': 'mag'},
    }
    with override_settings(CROSSMATCH_CATALOGS=[extra]):
        resp = client.get(DESCRIBE)
        body = resp.json()
        openapi_validate('describe_service', 200, body)

    (entry,) = body['catalogs']
    assert entry['release'] == 'Test R1'
    assert entry['catalog_source_id']['column'] == 'objid'
    assert 'objid' in entry['catalog_source_id']['description']
    assert entry['coverage_map']['footprint_moc_order'] is None
    assert entry['coverage_map']['resolution_arcmin'] is None
    assert entry['filterable_fields'][0]['unit'] == 'mag'


def test_describe_never_opens_a_catalog(client):
    lsdb = pytest.importorskip('lsdb')
    with mock.patch.object(lsdb, 'open_catalog', side_effect=AssertionError('opened')) as opened:
        assert client.get(DESCRIBE).status_code == 200
    opened.assert_not_called()


def test_describe_names_the_mcp_endpoint(client, openapi_validate):
    """Agents reading api/describe learn the chat connector exists (R14; F2)."""
    from chat_mcp.tools import TOOLS

    body = client.get(DESCRIBE).json()

    mcp = body['mcp']
    assert mcp['path'] == reverse('mcp') == '/mcp'
    assert mcp['method'] == 'POST'
    assert mcp['authentication'] == 'none'
    assert [t['name'] for t in mcp['tools']] == list(TOOLS)
    assert all(t['title'] for t in mcp['tools'])
    assert mcp['description'].strip()
    openapi_validate('describe_service', 200, body)


def test_describe_rejects_non_get_with_structured_error(client, openapi_validate):
    resp = client.post(DESCRIBE)

    assert resp.status_code == 405
    body = resp.json()
    assert body['code'] == 'method_not_allowed'
    openapi_validate('describe_service', 405, body)


# --- the searched caveat reaches the contract (R23) ---------------------------


def test_searched_description_carries_the_footprint_caveat(client):
    text = CODE_DESCRIPTIONS['searched']
    assert 'footprint' in text
    assert 'resolution' in text

    doc = client.get('/openapi.json').json()
    assert text in doc['components']['schemas']['CatalogOutcome']['description']


# --- footprint_moc_order setting ----------------------------------------------


def test_default_footprint_orders_match_the_served_coverage_maps():
    orders = {c['name']: c.get('footprint_moc_order') for c in settings.CROSSMATCH_CATALOGS}
    assert orders == DEFAULT_ORDERS


@pytest.mark.parametrize('value', [0, 8, 29, None])
def test_valid_footprint_orders_pass(value):
    _validate_footprint_moc_orders([{'name': 't', 'footprint_moc_order': value}])
    _validate_footprint_moc_orders([{'name': 't'}])


@pytest.mark.parametrize('value', [-1, 30, 8.0, '8', True])
def test_invalid_footprint_orders_raise(value):
    with pytest.raises(ImproperlyConfigured, match="'t'"):
        _validate_footprint_moc_orders([{'name': 't', 'footprint_moc_order': value}])


def _import_settings(**env):
    """Import ``project.settings`` in a fresh interpreter with ``env`` applied."""
    return subprocess.run(
        [
            sys.executable,
            '-c',
            'import json, project.settings as s; '
            'print(json.dumps([c.get("footprint_moc_order") '
            'for c in s.CROSSMATCH_CATALOGS]))',
        ],
        cwd=APP_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


def test_footprint_order_is_env_overridable():
    result = _import_settings(DES_FOOTPRINT_MOC_ORDER='7', SKYMAPPER_FOOTPRINT_MOC_ORDER='')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '[8, 7, 10, null]'


@pytest.mark.parametrize('raw', ['30', '-1', 'eight'])
def test_invalid_footprint_order_raises_at_import(raw):
    result = _import_settings(GAIA_FOOTPRINT_MOC_ORDER=raw)
    assert result.returncode != 0
    assert 'ImproperlyConfigured' in result.stderr
    assert 'GAIA_FOOTPRINT_MOC_ORDER' in result.stderr or 'gaia_dr3' in result.stderr
