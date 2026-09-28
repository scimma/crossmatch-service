"""U8 / R9, R24, R25, R26, R27; F1, F2; KTD15: agent-facing docs from one source.

``/llms.txt`` (llmstxt.org structure) and ``/api-docs.md`` are rendered from the
same builders as the OpenAPI document and the HTML ``/api-docs`` page, so every
configured fact agrees across the four and follows live settings. The agent
parity walks (F1, F2) discover every URL from ``/llms.txt`` and the OpenAPI
document, with no hard-coded paths.
"""

import json
import re
from typing import Any
from urllib.parse import urlsplit

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import Resolver404, resolve

from api.contract import CODE_DESCRIPTIONS, InputStatus, ObjectStatus
from core import provenance
from core.models import Alert
from core.healpix import radec_to_ipix
from tests.factories import (
    AlertDeliveryFactory,
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
)

LLMS = '/llms.txt'
MARKDOWN = '/api-docs.md'
_LINK = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')


def _links(text: str) -> list[tuple[str, str]]:
    return _LINK.findall(text)


def _path(url: str) -> str:
    return urlsplit(url).path


# --- /llms.txt structure (R25) -------------------------------------------------


def test_llms_txt_is_plain_text_in_llmstxt_structure(client):
    resp = client.get(LLMS)
    assert resp.status_code == 200
    assert resp['Content-Type'].startswith('text/plain')
    lines = resp.content.decode().splitlines()
    assert lines[0].startswith('# ')
    first_body = next(line for line in lines[1:] if line.strip())
    assert first_body.startswith('> ')
    assert any(line.startswith('## ') for line in lines)


def test_llms_txt_stays_under_10_kb(client):
    assert len(client.get(LLMS).content) < 10 * 1024


def test_llms_txt_points_at_openapi_markdown_describe_and_status(client):
    paths = {_path(url) for _, url in _links(client.get(LLMS).content.decode())}
    assert {'/openapi.json', MARKDOWN, '/api/describe', '/api/status'} <= paths


@pytest.mark.django_db
def test_llms_txt_links_use_https_behind_the_tls_proxy(client):
    """TLS terminates at the ingress, so the proxy's X-Forwarded-Proto sets the scheme."""
    text = client.get(LLMS, HTTP_X_FORWARDED_PROTO='https').content.decode()
    urls = [url for _, url in _links(text)]
    assert urls
    assert all(url.startswith('https://') for url in urls)
    plain = client.get(LLMS).content.decode()
    assert all(url.startswith('http://') for _, url in _links(plain))


@pytest.mark.django_db
def test_llms_txt_links_resolve_to_live_routes(client):
    links = _links(client.get(LLMS).content.decode())
    assert links
    for text, url in links:
        path = _path(url)
        try:
            resolve(path)
        except Resolver404:
            pytest.fail(f'{text!r} links to {url!r}, which is not a live route')
        if '{' not in path and path != '/api/lookup':
            assert client.get(path).status_code != 404, url


def test_llms_txt_lists_every_query_operation(client):
    text = client.get(LLMS).content.decode()
    doc = client.get('/openapi.json').json()
    for path, item in doc['paths'].items():
        for method, op in item.items():
            assert op['operationId'] in text, op['operationId']
            assert path in text, path


def test_llms_txt_carries_the_caveats(client):
    text = client.get(LLMS).content.decode()
    assert 'not evidence that the object failed a reliability cut' in text
    assert 'not a host association' in text
    assert 'not evidence of a hostless transient' in text
    assert 'advance between pages' in text
    assert 'nearest' in text


# --- /api-docs.md (R26) --------------------------------------------------------


def test_markdown_docs_are_served_with_describedby_link(client):
    resp = client.get(MARKDOWN)
    assert resp.status_code == 200
    assert resp['Content-Type'].startswith('text/markdown')
    assert resp['Link'] == '</llms.txt>; rel="describedby"'
    assert resp.content.decode().startswith('# ')


def test_markdown_docs_cover_every_operation_parameter_and_code(client):
    text = client.get(MARKDOWN).content.decode()
    doc = client.get('/openapi.json').json()
    for path, item in doc['paths'].items():
        for method, op in item.items():
            assert f'{method.upper()} {path}' in text
            assert op['operationId'] in text
            for param in op.get('parameters', []):
                assert f"`{param['name']}`" in text, (op['operationId'], param['name'])
    for code, meaning in CODE_DESCRIPTIONS.items():
        assert f'`{code}`' in text, code
        assert meaning in text, code


def test_markdown_docs_carry_catalog_meanings_and_caveats(client):
    text = client.get(MARKDOWN).content.decode()
    describe = client.get('/api/describe').json()
    for cat in describe['catalogs']:
        assert cat['catalog_source_id']['description'] in text
    assert 'not a host association' in text
    assert 'not evidence of a hostless transient' in text
    assert 'ICRS' in text


# --- R27: one source of configured facts ----------------------------------------

_SURFACES = ['/openapi.json', LLMS, MARKDOWN, '/api-docs']

_TEST_CATALOG = {
    'name': 'test_cat',
    'hats_url': 's3://nowhere',
    'release': 'Test Release R7',
    'source_id_column': 'objid',
    'ra_column': 'ra',
    'dec_column': 'dec',
    'payload_columns': ['ra', 'dec', 'mag'],
    'filter_columns': {'mag': 'mag'},
}


@pytest.mark.parametrize('url', _SURFACES)
def test_radius_change_reaches_every_surface(client, url):
    with override_settings(CROSSMATCH_RADIUS_ARCSEC=2.345):
        text = client.get(url).content.decode()
    assert '2.345 arcsec' in text
    default = client.get(url).content.decode()
    assert '2.345' not in default


@pytest.mark.parametrize('url', _SURFACES)
def test_catalog_list_change_reaches_every_surface(client, url):
    with override_settings(CROSSMATCH_CATALOGS=[_TEST_CATALOG]):
        text = client.get(url).content.decode()
    assert 'test_cat' in text
    assert 'Test Release R7' in text
    assert 'gaia_dr3' not in text


@pytest.mark.parametrize('url', _SURFACES)
def test_every_surface_agrees_on_configured_facts(client, url):
    text = client.get(url).content.decode()
    current = provenance.service_provenance()
    assert f"{current['crossmatch_radius_arcsec']} arcsec" in text
    assert current['service_version'] in text
    for cat in current['catalogs']:
        assert cat['release'] in text, cat
    for cut in current['reliability_cuts']:
        assert cut['broker'] in text, cut
    assert str(settings.MIN_DIASOURCE_RELIABILITY) in text


@override_settings(APP_VERSION='9.8.7', MIN_DIASOURCE_RELIABILITY=0.37)
@pytest.mark.parametrize('url', _SURFACES)
def test_version_and_cut_changes_reach_every_surface(client, url):
    text = client.get(url).content.decode()
    assert '9.8.7' in text
    assert '0.37' in text


# --- F1, F2: agent-parity walks -----------------------------------------------


def _discover(client) -> dict[str, Any]:
    """Find the OpenAPI document through /llms.txt, then every operation in it."""
    llms = client.get(LLMS).content.decode()
    openapi_url = next(url for text, url in _links(llms) if 'OpenAPI' in text)
    doc = client.get(_path(openapi_url)).json()
    operations = {}
    for path, item in doc['paths'].items():
        for method, op in item.items():
            operations[op['operationId']] = (method, path)
    return {'doc': doc, 'operations': operations}


def _call(client, operations, operation_id, body):
    method, path = operations[operation_id]
    assert method == 'post'
    return client.post(path, data=json.dumps(body), content_type='application/json')


def _assert_methods_sentence(body: dict[str, Any]) -> None:
    """The provenance a methods sentence needs, all in one response."""
    prov = body['provenance']
    assert prov['service_version'] == provenance.service_version()
    assert prov['crossmatch_radius_arcsec'] == provenance.crossmatch_radius_arcsec()
    assert {c['release'] for c in prov['catalogs']} == {
        c['release'] for c in provenance.catalog_releases()
    }
    cuts = {c['broker']: c for c in prov['reliability_cuts']}
    assert set(cuts) == {'antares', 'lasair', 'pittgoogle'}
    for cut in cuts.values():
        assert {'min_reliability', 'enforced_by', 'status'} <= set(cut)


@pytest.mark.django_db
def test_f1_agent_vets_broker_candidates(client, openapi_validate):
    """F1: count significant-parallax Gaia matches, then fetch them, via discovery."""
    star = ObjectCrossmatchRecordFactory()
    AlertDeliveryFactory(alert=star.alert)
    CatalogMatchFactory(
        alert=star.alert, catalog_name='gaia_dr3',
        catalog_payload={'parallax': 10.0, 'parallax_error': 0.5},
    )
    galaxy = ObjectCrossmatchRecordFactory()
    CatalogMatchFactory(
        alert=galaxy.alert, catalog_name='gaia_dr3',
        catalog_payload={'parallax': 0.1, 'parallax_error': 0.5},
    )
    unseen = 4_242_424_242
    ids = [
        star.alert.lsst_diaObject_diaObjectId,
        galaxy.alert.lsst_diaObject_diaObjectId,
        unseen,
    ]

    found = _discover(client)
    filter_names = found['doc']['components']['schemas']['Filters']['properties']
    parallax_min = next(n for n in filter_names if n.endswith('parallax_over_error_min'))
    inputs = [{'kind': 'id', 'diaObjectId': str(i)} for i in ids]
    filters = {parallax_min: 5}

    counted = _call(client, found['operations'], 'lookup_objects', {
        'inputs': inputs, 'filters': filters, 'response': 'count',
    })
    assert counted.status_code == 200
    counts = counted.json()
    openapi_validate('lookup_objects', 200, counts)
    assert counts['counts']['objects']['qualifying'] == 1
    assert counts['counts']['objects']['by_status'][ObjectStatus.NOT_IN_SERVICE] == 1

    fetched = _call(client, found['operations'], 'lookup_objects', {
        'inputs': inputs, 'filters': filters, 'detail': 'full',
    })
    assert fetched.status_code == 200
    body = fetched.json()
    openapi_validate('lookup_objects', 200, body)
    assert [r['status'] for r in body['results']] == [
        ObjectStatus.COINCIDENT_SOURCES, ObjectStatus.COINCIDENT_SOURCES,
        ObjectStatus.NOT_IN_SERVICE,
    ]
    qualifying = [r for r in body['results'] if r['qualifies']]
    assert [r['objects'][0]['diaObjectId'] for r in qualifying] == [ids[0]]
    match = qualifying[0]['objects'][0]['matches'][0]
    assert match['catalog_payload']['parallax'] == 10.0
    _assert_methods_sentence(body)


@pytest.mark.django_db
def test_f2_agent_checks_a_spectroscopic_target_list(client, openapi_validate):
    """F2: positions and TNS names as one batch, one result per target, via discovery."""
    ra, dec = 150.0, 2.0
    alert = AlertFactory(
        ra_deg=ra, dec_deg=dec, healpix_ipix=radec_to_ipix(ra, dec),
        status=Alert.Status.MATCHED,
    )
    ObjectCrossmatchRecordFactory(alert=alert)
    CatalogMatchFactory(
        alert=alert, catalog_name='des_y6_gold',
        catalog_payload={'dnf_z': 0.12, 'dnf_zsigma': 0.02},
    )

    found = _discover(client)
    inputs = [
        {'kind': 'position', 'ra': ra, 'dec': dec, 'radius_arcsec': 2},
        {'kind': 'position', 'ra': 10.0, 'dec': -10.0, 'radius_arcsec': 2},
        {'kind': 'tns', 'name': 'SN 2026abc'},
    ]
    resp = _call(client, found['operations'], 'lookup_objects', {
        'inputs': inputs, 'detail': 'full',
    })
    assert resp.status_code == 200
    body = resp.json()
    openapi_validate('lookup_objects', 200, body)

    assert [r['index'] for r in body['results']] == [0, 1, 2]
    assert body['results'][0]['status'] == InputStatus.OBJECTS_FOUND
    assert body['results'][1]['status'] == InputStatus.NO_RUBIN_OBJECT
    assert body['results'][2]['status'] in (
        InputStatus.RESOLVER_UNAVAILABLE, InputStatus.TNS_NAME_NOT_FOUND,
    )
    source = body['results'][0]['objects'][0]['matches'][0]
    assert source['catalog_name'] == 'des_y6_gold'
    assert source['catalog_payload']['dnf_z'] == 0.12
    # The contract keeps a nuclear-host redshift apart from host association.
    match_schema = found['doc']['components']['schemas']['LookupMatch']
    assert 'not a host association' in match_schema['description']
    _assert_methods_sentence(body)
