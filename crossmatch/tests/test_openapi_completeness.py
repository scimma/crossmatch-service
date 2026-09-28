"""U8 / R22, R23, R24, R9; KTD14: the OpenAPI document is complete and self-describing.

Completeness runs in both directions: every code the code base can emit (taken
from the contract enums, ``ERROR_CODES``, the filter registry, and the other
vocabulary constants, not by grepping source) appears in the served document,
and every code the document lists is one the code base emits. Every operation
is described, every numeric field names its unit, and the field descriptions
carry the per-catalog meaning of ``catalog_source_id``, the
nearest-source-per-catalog rule, frames and epochs, and the R9 and R24 caveats.
"""

from typing import Any, Iterator

import pytest
from django.conf import settings
from django.test import override_settings

from api import contract
from api.discovery import DATABASE_STATES, TNS_RESOLUTION_STATES
from api.errors import ERROR_CODES
from api.filters import RESPONSE_MODES, filter_parameters
from api.lookup import (
    BASIS_BEST_GUESS,
    BASIS_RECORDED,
    INPUT_KINDS,
    PROVENANCE_NOT_RECORDED,
    PROVENANCE_RECORDED,
)
from api.positions import TRUNCATION_VALUES
from api.service import DETAIL_LEVELS, TIME_FIELDS
from core.models import CutEnforcedBy, CutStatus

OPENAPI_URL = '/openapi.json'
_METHODS = ('get', 'put', 'post', 'delete', 'patch', 'head', 'options')
_NUMERIC = {'number', 'integer'}


def _document(client) -> dict[str, Any]:
    resp = client.get(OPENAPI_URL)
    assert resp.status_code == 200
    return resp.json()


def _walk(node: Any, where: str = '#') -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield every dict in the document with its JSON-pointer-ish location."""
    if isinstance(node, dict):
        yield where, node
        for key, value in node.items():
            yield from _walk(value, f'{where}/{key}')
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f'{where}/{i}')


def _operations(doc: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in doc['paths'].items():
        for method in _METHODS:
            if method in item:
                yield path, method, item[method]


def _is_numeric(schema: dict[str, Any]) -> bool:
    kind = schema.get('type')
    if isinstance(kind, list):
        return bool(_NUMERIC.intersection(kind))
    return kind in _NUMERIC


def _emittable_codes() -> set[str]:
    """Every code value the service can put in a response, from the code base."""
    codes: set[str] = set()
    codes.update(ERROR_CODES)
    codes.update(contract.ErrorCode.values)
    codes.update(contract.ObjectStatus.values)
    codes.update(contract.InputStatus.values)
    codes.update(contract.CATALOG_OUTCOME_VALUES)
    codes.update(contract.QualifiesReason.values)
    codes.update(CutEnforcedBy.values)
    codes.update(CutStatus.values)
    codes.update(TRUNCATION_VALUES)
    codes.update({BASIS_RECORDED, BASIS_BEST_GUESS})
    codes.update({PROVENANCE_RECORDED, PROVENANCE_NOT_RECORDED})
    codes.update(DATABASE_STATES)
    codes.update(TNS_RESOLUTION_STATES)
    codes.update(DETAIL_LEVELS)
    codes.update(TIME_FIELDS)
    codes.update(RESPONSE_MODES)
    codes.update(INPUT_KINDS)
    codes.update(str(cat['name']) for cat in settings.CROSSMATCH_CATALOGS)
    codes.update(p.bound for p in filter_parameters() if p.bound is not None)
    return codes


def _documented_codes(doc: dict[str, Any]) -> set[str]:
    """Every string value the document lists in an ``enum`` or ``const``."""
    codes: set[str] = set()
    for _, node in _walk(doc):
        for value in node.get('enum') or []:
            if isinstance(value, str):
                codes.add(value)
        if isinstance(node.get('const'), str):
            codes.add(node['const'])
    return codes


# --- codes: both directions ---------------------------------------------------


def test_every_emittable_code_is_documented(client):
    missing = _emittable_codes() - _documented_codes(_document(client))
    assert not missing, f'codes the service can emit but the document omits: {missing}'


def test_every_documented_code_is_emitted_somewhere(client):
    extra = _documented_codes(_document(client)) - _emittable_codes()
    assert not extra, f'codes the document lists but the service never emits: {extra}'


def test_every_contract_code_is_defined_where_it_is_listed(client):
    """Each code enum's description defines every value it lists."""
    schemas = _document(client)['components']['schemas']
    for name in (
        'ObjectStatus', 'InputStatus', 'CatalogOutcome', 'ErrorCode',
        'QualifiesReason', 'CutEnforcedBy', 'CutStatus',
    ):
        for value in schemas[name]['enum']:
            assert f'`{value}`: {contract.CODE_DESCRIPTIONS[value]}' in (
                schemas[name]['description']
            ), (name, value)


def test_every_filter_parameter_is_documented_on_every_filtered_operation(client):
    doc = _document(client)
    names = {p.name for p in filter_parameters()}
    assert set(doc['components']['schemas']['Filters']['properties']) == names
    for path, method, op in _operations(doc):
        if op['operationId'] in ('get_object', 'cone_search', 'resolve_tns'):
            params = {p['name'] for p in op.get('parameters', [])}
            assert names <= params, (path, names - params)


# --- descriptions and units ---------------------------------------------------


def test_every_operation_has_a_summary_and_description(client):
    for path, method, op in _operations(_document(client)):
        assert op.get('summary', '').strip(), (method, path)
        assert op.get('description', '').strip(), (method, path)


def test_every_numeric_field_names_its_unit(client):
    missing = [
        where for where, node in _walk(_document(client))
        if _is_numeric(node) and not str(node.get('x-unit', '')).strip()
    ]
    assert not missing, f'numeric schemas with no x-unit: {missing}'


def test_angular_fields_use_degrees_or_arcsec(client):
    """Positions are in deg, radii and separations in arcsec."""
    doc = _document(client)
    named = [
        (where.rsplit('/', 1)[-1], where, node)
        for where, node in _walk(doc) if _is_numeric(node)
    ]
    for _, _, op in _operations(doc):
        for param in op.get('parameters', []):
            if _is_numeric(param['schema']):
                named.append((param['name'], param['name'], param['schema']))
    for name, where, node in named:
        if name in ('ra', 'dec'):
            assert node['x-unit'] == 'deg', where
        if name.endswith('_arcsec') or name.endswith('_arcsec_max'):
            assert node['x-unit'] == 'arcsec', where


def test_filter_units_come_from_the_registry(client):
    schemas = _document(client)['components']['schemas']
    for param in filter_parameters():
        if param.unit is not None:
            assert schemas['Filters']['properties'][param.name]['x-unit'] == param.unit


# --- R23: catalog_source_id, nearest rule, frames and epochs -----------------


@pytest.mark.parametrize('schema', ['MatchSummary', 'PublishedMatch'])
def test_catalog_source_id_states_its_meaning_per_catalog(client, schema):
    field = _document(client)['components']['schemas'][schema]['properties'][
        'catalog_source_id'
    ]
    text = field['description']
    for cat in settings.CROSSMATCH_CATALOGS:
        assert f"`{cat['name']}`" in text, cat['name']
        assert cat['source_id_column'] in text, cat['name']
    assert 'unique only within one catalog_name' in text


def test_catalog_source_id_meaning_follows_the_live_catalog_list(client):
    extra = {
        'name': 'test_cat',
        'hats_url': 's3://nowhere',
        'release': 'Test R1',
        'source_id_column': 'objid',
        'ra_column': 'ra',
        'dec_column': 'dec',
        'payload_columns': ['ra', 'dec'],
    }
    with override_settings(CROSSMATCH_CATALOGS=[extra]):
        text = _document(client)['components']['schemas']['MatchSummary'][
            'properties'
        ]['catalog_source_id']['description']
    assert '`test_cat`' in text
    assert 'objid' in text
    assert 'gaia_dr3' not in text


@pytest.mark.parametrize('schema', ['MatchSummary', 'PublishedMatch', 'LookupMatch'])
def test_match_schemas_state_the_nearest_source_rule(client, schema):
    text = _document(client)['components']['schemas'][schema]['description']
    assert 'nearest' in text
    assert 'at most one coincident source per catalog' in text


def test_positions_state_frames_and_epochs(client):
    schemas = _document(client)['components']['schemas']
    obj_ra = schemas['LookupObject']['properties']['ra']['description']
    assert 'ICRS' in obj_ra
    source_ra = schemas['PublishedMatch']['properties']['ra']['description']
    assert 'ref_epoch' in source_ra  # Gaia DR3 positions carry their epoch
    assert 'J2016.0' in source_ra
    assert 'raj2000' in source_ra  # SkyMapper coordinate column names
    assert 'proper motion' in source_ra


def test_separation_states_what_it_separates(client):
    schemas = _document(client)['components']['schemas']
    for name in ('MatchSummary', 'PublishedMatch'):
        text = schemas[name]['properties']['separation_arcsec']['description']
        assert 'arcsec' in text
        assert 'Rubin object' in text


# --- R9, R24, paging caveats ---------------------------------------------------


def test_info_carries_the_r9_and_r24_caveats(client):
    text = _document(client)['info']['description']
    assert 'not evidence that the object failed a reliability cut' in text
    assert 'not a host association' in text
    assert 'not evidence of a hostless transient' in text


def test_cone_cursor_notes_that_status_can_advance(client):
    doc = _document(client)
    op = doc['paths']['/api/cone']['get']
    cursor = next(p for p in op['parameters'] if p['name'] == 'cursor')
    assert 'status' in cursor['description']
    assert 'advance between pages' in cursor['description']


def test_provenance_description_states_the_reliability_cuts(client):
    text = _document(client)['components']['schemas']['Provenance']['description']
    for cut in ('antares', 'lasair', 'pittgoogle'):
        assert cut in text
    assert str(settings.MIN_DIASOURCE_RELIABILITY) in text
