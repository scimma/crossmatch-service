"""U1 / R8, R17, R22, R28; KTD2, KTD4, KTD14: the shared response contract.

Covers the fixed snake_case codes (stored ones owned by ``core.models``,
response-only ones by ``api.contract``), the structured error type with the
legacy ``error`` key, the provenance envelope, the 64-bit ID helpers, the
hand-built OpenAPI 3.1 document served at ``/openapi.json``, and the
``openapi_validate`` conformance fixture every API test uses.
"""

import json

import jsonschema
import pytest
from django.test import override_settings
from openapi_spec_validator import OpenAPIV31SpecValidator
from openapi_spec_validator import validate as validate_spec

from api import contract
from api.errors import ERROR_CODES, ApiError, InvalidQuery
from core import provenance
from core.models import CatalogSearchOutcome, CutEnforcedBy, CutStatus
from tests.factories import AlertFactory, CatalogMatchFactory

OPENAPI_URL = '/openapi.json'
RECENT_URL = '/api/recent-crossmatches'


# --- codes (KTD2) -------------------------------------------------------------


def test_stored_codes_are_fixed_snake_case_values():
    assert CatalogSearchOutcome.values == [
        'searched',
        'outside_footprint',
        'skipped_read_failure',
        'not_searched_invalid_position',
    ]
    assert CutEnforcedBy.values == ['service', 'broker']
    assert CutStatus.values == ['service_setting', 'declared', 'not_declared']


def test_response_codes_are_fixed_snake_case_values():
    assert contract.ObjectStatus.values == [
        'not_in_service',
        'crossmatch_pending',
        'coincident_sources',
        'no_coincident_source',
        'not_searched',
    ]
    assert contract.InputStatus.values == [
        'no_rubin_object',
        'objects_found',
        'tns_name_not_found',
        'resolver_unavailable',
        'invalid_input',
    ]
    assert contract.ReadTimeCatalogOutcome.values == [
        'not_recorded',
        'not_in_service_at_crossmatch',
    ]


def test_error_code_choices_match_the_errors_module():
    assert tuple(contract.ErrorCode.values) == ERROR_CODES


def test_catalog_outcomes_reuse_stored_codes():
    assert contract.CATALOG_OUTCOME_VALUES == (
        CatalogSearchOutcome.values + contract.ReadTimeCatalogOutcome.values
    )


def test_every_code_has_a_description():
    for enum_cls in (
        contract.ObjectStatus,
        contract.InputStatus,
        contract.ReadTimeCatalogOutcome,
        contract.ErrorCode,
        CatalogSearchOutcome,
        CutEnforcedBy,
        CutStatus,
    ):
        for value in enum_cls.values:
            assert contract.CODE_DESCRIPTIONS[value].strip(), value


def test_not_in_service_description_disclaims_reliability_cut():
    """R9 wording lives in the enum description, not the value (KTD2)."""
    text = contract.CODE_DESCRIPTIONS['not_in_service'].lower()
    assert 'reliability' in text
    assert 'not evidence' in text


# --- errors (KTD2, R28) -------------------------------------------------------


def test_error_serializes_with_code_message_param_retryable_and_legacy_error():
    err = ApiError(
        'page_size is not an integer', code='invalid_parameter', param='page_size'
    )
    assert err.to_dict() == {
        'error': 'page_size is not an integer',
        'code': 'invalid_parameter',
        'message': 'page_size is not an integer',
        'param': 'page_size',
        'retryable': False,
    }


def test_error_with_several_params_uses_params_list():
    err = ApiError(
        'match filters span more than one catalog',
        code='invalid_parameter',
        params=['gaia_parallax_min', 'des_dnf_z_max'],
    )
    body = err.to_dict()
    assert body['params'] == ['gaia_parallax_min', 'des_dnf_z_max']
    assert 'param' not in body


def test_error_rejects_both_param_and_params():
    with pytest.raises(ValueError):
        ApiError('x', code='invalid_parameter', param='a', params=['b'])


def test_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        ApiError('x', code='no_such_code')


def test_retryable_error_response_carries_retry_after():
    err = ApiError(
        'database unavailable',
        code='service_unavailable',
        status=503,
        retryable=True,
        retry_after=30,
    )
    resp = contract.error_response(err)
    assert resp.status_code == 503
    assert resp['Retry-After'] == '30'
    assert json.loads(resp.content)['retryable'] is True


def test_invalid_query_stays_a_value_error_with_its_message():
    exc = InvalidQuery('end must not be earlier than start')
    assert isinstance(exc, ValueError)
    assert isinstance(exc, ApiError)
    assert str(exc) == 'end must not be earlier than start'
    assert exc.status == 400
    assert exc.to_dict()['code'] == 'invalid_parameter'
    assert exc.to_dict()['error'] == 'end must not be earlier than start'


def test_invalid_query_accepts_param():
    exc = InvalidQuery('bad detail', param='detail')
    assert exc.to_dict()['param'] == 'detail'


# --- envelope (R17) -----------------------------------------------------------


@override_settings(CROSSMATCH_RADIUS_ARCSEC=2.5)
def test_envelope_adds_live_provenance():
    body = contract.envelope({'objects': []})
    assert body['objects'] == []
    assert body['provenance'] == provenance.service_provenance()
    assert body['provenance']['crossmatch_radius_arcsec'] == 2.5


def test_envelope_rejects_a_body_that_already_has_provenance():
    with pytest.raises(ValueError):
        contract.envelope({'provenance': {}})


# --- 64-bit IDs (KTD4) --------------------------------------------------------

BIG_ID = 2**62 + 1  # above 2^53, so a float round trip would corrupt it


@pytest.mark.parametrize('raw', [BIG_ID, str(BIG_ID)])
def test_dia_object_id_accepts_int_or_decimal_string(raw):
    assert contract.parse_dia_object_id(raw) == BIG_ID


@pytest.mark.parametrize(
    'raw',
    [True, 1.0, float(BIG_ID), '1e5', '0x10', '', ' 12', '+12', '-1', -1, 2**63, str(2**63), None, [1]],
)
def test_dia_object_id_rejects_non_int64_values(raw):
    with pytest.raises(InvalidQuery) as info:
        contract.parse_dia_object_id(raw, param='ids[0]')
    assert info.value.to_dict()['param'] == 'ids[0]'


def test_dia_object_id_fields_emit_int_and_string():
    assert contract.dia_object_id_fields(BIG_ID) == {
        'diaObjectId': BIG_ID,
        'diaObjectId_str': str(BIG_ID),
    }


# --- OpenAPI document (KTD14, R22) --------------------------------------------


def test_openapi_document_is_served_as_json(client):
    resp = client.get(OPENAPI_URL)
    assert resp.status_code == 200
    assert resp['Content-Type'].startswith('application/json')
    assert resp.json()['openapi'].startswith('3.1')


def test_served_openapi_document_passes_3_1_spec_validation(client):
    doc = client.get(OPENAPI_URL).json()
    validate_spec(doc, cls=OpenAPIV31SpecValidator)


def test_openapi_document_code_enums_match_contract(client):
    schemas = client.get(OPENAPI_URL).json()['components']['schemas']
    assert schemas['ObjectStatus']['enum'] == contract.ObjectStatus.values
    assert schemas['InputStatus']['enum'] == contract.InputStatus.values
    assert schemas['CatalogOutcome']['enum'] == list(contract.CATALOG_OUTCOME_VALUES)
    assert schemas['ErrorCode']['enum'] == contract.ErrorCode.values
    assert schemas['CutEnforcedBy']['enum'] == CutEnforcedBy.values
    assert schemas['CutStatus']['enum'] == CutStatus.values
    for name in ('ObjectStatus', 'InputStatus', 'CatalogOutcome', 'ErrorCode'):
        for value in schemas[name]['enum']:
            assert f'`{value}`' in schemas[name]['description'], (name, value)


@override_settings(CROSSMATCH_RADIUS_ARCSEC=4.0)
def test_openapi_document_reads_live_provenance(client):
    """R27: the document is built per request from the provenance builder."""
    schemas = client.get(OPENAPI_URL).json()['components']['schemas']
    assert schemas['Provenance']['example']['crossmatch_radius_arcsec'] == 4.0
    assert '4.0 arcsec' in schemas['Provenance']['description']


def test_provenance_component_validates_builder_output(client):
    doc = client.get(OPENAPI_URL).json()
    root = {**doc, '$ref': '#/components/schemas/Provenance'}
    jsonschema.Draft202012Validator(root).validate(provenance.service_provenance())


def test_operation_ids_are_unique(client):
    doc = client.get(OPENAPI_URL).json()
    ids = [
        op['operationId']
        for item in doc['paths'].values()
        for op in item.values()
        if isinstance(op, dict) and 'operationId' in op
    ]
    assert len(ids) == len(set(ids))
    assert 'recent_crossmatches' in ids


def test_openapi_rejects_non_get_with_structured_error(client, openapi_validate):
    resp = client.post(OPENAPI_URL)
    assert resp.status_code == 405
    body = resp.json()
    assert body['code'] == 'method_not_allowed'
    assert body['error'] == body['message']
    openapi_validate('get_openapi', 405, body)


# --- conformance fixture ------------------------------------------------------


def test_fixture_validates_openapi_response(client, openapi_validate):
    openapi_validate('get_openapi', 200, client.get(OPENAPI_URL).json())


def test_fixture_rejects_a_nonconforming_body(openapi_validate):
    with pytest.raises(jsonschema.ValidationError):
        openapi_validate('get_openapi', 405, {'error': 'x'})


def test_fixture_fails_loudly_on_undocumented_status(openapi_validate):
    with pytest.raises(AssertionError, match='418'):
        openapi_validate('get_openapi', 418, {})


def test_fixture_fails_loudly_on_unknown_operation(openapi_validate):
    with pytest.raises(AssertionError, match='no_such_operation'):
        openapi_validate('no_such_operation', 200, {})


@pytest.mark.django_db
def test_recent_crossmatches_responses_conform_unchanged(client, openapi_validate):
    """The existing operation is described as-is; its behavior is untouched (U9)."""
    CatalogMatchFactory(alert=AlertFactory())
    for detail in ('ids', 'position', 'matches', 'full'):
        ok = client.get(RECENT_URL, {'detail': detail})
        assert ok.status_code == 200
        assert ok.json()['count'] == 1
        assert 'provenance' not in ok.json()
        openapi_validate('recent_crossmatches', 200, ok.json())

    bad = client.get(RECENT_URL, {'detail': 'bogus'})
    assert bad.status_code == 400
    assert list(bad.json()) == ['error']
    openapi_validate('recent_crossmatches', 400, bad.json())

    not_allowed = client.post(RECENT_URL)
    assert not_allowed.status_code == 405
    openapi_validate('recent_crossmatches', 405, not_allowed.json())
