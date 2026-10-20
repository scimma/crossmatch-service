"""U6: the MCP tools through ``POST /mcp`` (R1-R4, R8, R13, R15; KTD5,
KTD14, KTD15; F1).

Each tool call runs under the request guard and answers with one text block
holding the JSON of the U5 projection. Argument problems and guard errors are
``isError`` tool results; each call logs one ``mcp tool call`` line.
"""

import json
import time
import uuid

import pytest
from django.db import connection
from django.test import override_settings
from structlog.testing import capture_logs

from api.discovery import describe_service
from api.guard import sql_phase
from api.lookup import lookup_objects
from chat_mcp import protocol
from chat_mcp.identifiers import classify_identifiers
from chat_mcp.projection import project_lookup
from tests.factories import AlertFactory
from tests.test_chat_mcp_projection import (
    ARCSEC,
    BASE_ID,
    alert_at,
    crossmatched,
    current_snapshot,
    tns_object,
)

MCP_URL = '/mcp'


@pytest.fixture(autouse=True)
def locmem_cache(settings):
    """A fresh locmem cache per test: tools/call touches the rate limiter's
    cache (U7), and the documented test run has no Valkey."""
    settings.CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': f'mcp-{uuid.uuid4()}',
    }}


def call_tool(client, name, arguments, **headers):
    body = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments},
    }
    resp = client.post(
        MCP_URL, json.dumps(body), content_type='application/json', headers=headers,
    )
    assert resp.status_code == 200, resp.content
    return resp.json()['result']


def answer(result):
    """The JSON of a tool result's single text block."""
    [block] = result['content']
    assert block['type'] == 'text'
    return json.loads(block['text'])


def tool_logs(logs):
    return [entry for entry in logs if entry['event'] == 'mcp tool call']


# --- lookup_rubin_transients (F1) ---

@pytest.mark.django_db
def test_f1_tns_name_lookup_returns_the_projection_as_one_text_block(client):
    current_snapshot()
    tns_object('2026abc', 150.0, 2.0, objid=777)
    crossmatched(BASE_ID, 150.0, 2.0 + 0.2 * ARCSEC)

    result = call_tool(client, 'lookup_rubin_transients', {'identifiers': ['SN 2026abc']})

    assert result['isError'] is False
    assert 'structuredContent' not in result
    classified = classify_identifiers(['SN 2026abc'])
    expected = project_lookup(
        classified, lookup_objects(inputs=classified.lookup_inputs(), detail='full'),
    )
    got = answer(result)
    assert got == json.loads(json.dumps(expected))
    [obj] = got['results'][0]['objects']
    assert obj['diaObjectId'] == str(BASE_ID)


@pytest.mark.django_db
def test_only_unrecognized_identifiers_are_answered_without_a_lookup(client):
    result = call_tool(client, 'lookup_rubin_transients', {'identifiers': ['ZTF26aaaaaaa']})

    assert result['isError'] is False
    [entry] = answer(result)['results']
    assert entry['status'] == 'unrecognized_identifier'
    assert entry['reason']


@pytest.mark.django_db
def test_101_identifiers_is_an_error_naming_the_100_limit(client):
    ids = [str(BASE_ID + n) for n in range(101)]

    result = call_tool(client, 'lookup_rubin_transients', {'identifiers': ids})

    assert result['isError'] is True
    error = answer(result)['error']
    assert error['code'] == 'invalid_arguments'
    assert '100' in error['message']


@pytest.mark.django_db
@pytest.mark.parametrize('arguments', [
    {'identifiers': []},
    {'identifiers': '2026abc'},
    {},
])
def test_an_empty_or_non_list_identifiers_argument_is_an_error(client, arguments):
    result = call_tool(client, 'lookup_rubin_transients', arguments)

    assert result['isError'] is True
    assert answer(result)['error']['code'] == 'invalid_arguments'


@pytest.mark.django_db
def test_25_identifiers_are_accepted_and_truncated_to_the_chat_maximum(client):
    ids = [BASE_ID + n for n in range(25)]
    for oid in ids:
        crossmatched(oid)

    result = call_tool(
        client, 'lookup_rubin_transients', {'identifiers': [str(i) for i in ids]},
    )

    assert result['isError'] is False
    got = answer(result)
    assert got['requested'] == 25
    assert got['objects_found'] == 25
    assert got['objects_returned'] == 20
    assert got['truncated'] is True
    assert got['truncation']['full_results']


# --- search_rubin_transients_near_position ---

@pytest.mark.django_db
def test_radius_above_the_cone_maximum_is_an_error_naming_the_maximum(client):
    result = call_tool(client, 'search_rubin_transients_near_position', {
        'ra_deg': 150.0, 'dec_deg': 2.0, 'radius_arcsec': 61,
    })

    assert result['isError'] is True
    error = answer(result)['error']
    assert error['code'] == 'invalid_arguments'
    assert '60' in error['message'] and 'radius_arcsec' in error['message']


@pytest.mark.django_db
@pytest.mark.parametrize('arguments, named', [
    ({'ra_deg': 150.0, 'dec_deg': 91}, 'dec_deg'),
    ({'ra_deg': 361, 'dec_deg': 0}, 'ra_deg'),
    ({'ra_deg': '150', 'dec_deg': 2.0}, 'ra_deg'),
    ({'dec_deg': 2.0}, 'ra_deg'),
])
def test_an_invalid_position_is_an_error_naming_the_argument(client, arguments, named):
    result = call_tool(client, 'search_rubin_transients_near_position', arguments)

    assert result['isError'] is True
    error = answer(result)['error']
    assert error['code'] == 'invalid_arguments'
    assert named in error['message']


@pytest.mark.django_db
def test_a_position_at_ra_359_99_finds_an_object_at_ra_0_01(client):
    # At Dec 80 the 0.02 deg RA gap is about 12.5 arcsec on the sky.
    alert_at(0.01, 80.0, lsst_diaObject_diaObjectId=BASE_ID)

    result = call_tool(client, 'search_rubin_transients_near_position', {
        'ra_deg': 359.99, 'dec_deg': 80.0, 'radius_arcsec': 20,
    })

    assert result['isError'] is False
    got = answer(result)
    assert [obj['diaObjectId'] for obj in got['objects']] == [str(BASE_ID)]
    assert got['order'] == 'nearest_first'


@pytest.mark.django_db
def test_radius_defaults_when_omitted(client):
    from chat_mcp.tools import DEFAULT_RADIUS_ARCSEC

    result = call_tool(client, 'search_rubin_transients_near_position', {
        'ra_deg': 150.0, 'dec_deg': 2.0,
    })

    assert result['isError'] is False
    assert answer(result)['radius_arcsec'] == DEFAULT_RADIUS_ARCSEC


# --- describe_crossmatch_service ---

@pytest.mark.django_db
def test_describe_reports_the_catalogs_status_and_latest_ingest(client):
    alert = AlertFactory()

    result = call_tool(client, 'describe_crossmatch_service', {})

    assert result['isError'] is False
    got = answer(result)
    assert [c['name'] for c in got['catalogs']] == [
        c['name'] for c in describe_service()['catalogs']
    ]
    assert got['database'] == 'ok'
    assert got['latest_alert_ingest'] == alert.ingest_time.isoformat()


# --- the request guard (R13, KTD15) ---

@pytest.mark.django_db(transaction=True)
@override_settings(API_REQUEST_BUDGET_SECONDS=0.3)
def test_a_call_over_the_budget_is_query_too_expensive_in_a_read_only_txn(
    client, monkeypatch,
):
    seen = {}

    def slow_lookup(**kwargs):
        with sql_phase():
            with connection.cursor() as cursor:
                cursor.execute('SHOW transaction_read_only')
                seen['read_only'] = cursor.fetchone()[0]
                cursor.execute('SELECT pg_sleep(5)')
        return {}

    monkeypatch.setattr('chat_mcp.tools.lookup_objects', slow_lookup)
    began = time.monotonic()

    with capture_logs() as logs:
        result = call_tool(client, 'lookup_rubin_transients', {'identifiers': ['2026abc']})

    assert time.monotonic() - began < 3
    assert seen['read_only'] == 'on'
    assert result['isError'] is True
    error = answer(result)['error']
    assert error['code'] == 'query_too_expensive'
    assert error['message']
    [line] = tool_logs(logs)
    assert line['outcome'] == 'query_too_expensive'


# --- the log line (R15, KTD14) ---

@pytest.mark.django_db
def test_each_tool_call_logs_one_line_without_the_session_id(client):
    crossmatched(BASE_ID)
    session_id = protocol.issue_session_id()
    token = protocol.verify_session_id(session_id)

    with capture_logs() as logs:
        call_tool(
            client, 'lookup_rubin_transients',
            {'identifiers': [str(BASE_ID), 'nonsense!']},
            **{'Mcp-Session-Id': session_id, 'Mcp-Protocol-Version': '2025-06-18'},
        )
        call_tool(client, 'search_rubin_transients_near_position', {
            'ra_deg': 150.0, 'dec_deg': 2.0, 'radius_arcsec': 5,
        })
        call_tool(client, 'describe_crossmatch_service', {})
        call_tool(client, 'lookup_rubin_transients', {'identifiers': []})

    lines = tool_logs(logs)
    assert [line['tool'] for line in lines] == [
        'lookup_rubin_transients',
        'search_rubin_transients_near_position',
        'describe_crossmatch_service',
        'lookup_rubin_transients',
    ]
    lookup_line, near_line, describe_line, invalid_line = lines
    assert lookup_line['identifiers'] == 2
    assert lookup_line['objects_found'] == 1
    assert lookup_line['objects_returned'] == 1
    assert lookup_line['outcome'] == 'ok'
    assert lookup_line['truncated'] is False
    assert lookup_line['protocol_version'] == '2025-06-18'
    assert lookup_line['client_class'] == 'direct'
    assert lookup_line['session'] == protocol.session_hash(token)
    assert near_line['objects_found'] == 1 and near_line['outcome'] == 'ok'
    assert near_line['session'] is None
    assert describe_line['outcome'] == 'ok'
    assert invalid_line['outcome'] == 'invalid_arguments'
    for line in lines:
        for field in ('total_seconds', 'sql_seconds', 'sql_phases',
                      'python_seconds', 'budget_seconds'):
            assert field in line
    flat = json.dumps(lines, default=str)
    assert session_id not in flat and token not in flat


# --- summarize_rubin_transients_by_night (alert window statistics U6) ---

SUMMARY_TOOL = 'summarize_rubin_transients_by_night'


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin the tool's clock to the summary-service fixture's ``NOW``."""
    from tests.test_summary_service import NOW

    monkeypatch.setattr('chat_mcp.tools._now', lambda: NOW)
    return NOW


def _summary_objects(count=3):
    from tests.test_summary_service import _obj

    ids = [BASE_ID + 700 + n for n in range(count)]
    for n, oid in enumerate(ids):
        _obj(oid=oid, reliability=0.9 - n * 0.1, brokers=('pittgoogle',),
             matches=('des_y6_gold',) if n == 0 else ())
    return ids


@pytest.mark.django_db
@override_settings(MCP_MAX_OBJECTS=7, SUMMARY_MAX_NIGHTS=12)
def test_summary_schema_enums_and_maximums_come_from_settings(client):
    from api.summary import broker_names

    resp = client.post(MCP_URL, json.dumps(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
    ), content_type='application/json')
    tools = {t['name']: t for t in resp.json()['result']['tools']}

    schema = tools[SUMMARY_TOOL]['inputSchema']['properties']
    assert schema['broker']['enum'] == broker_names()
    from django.conf import settings
    assert schema['catalog']['enum'] == [c['name'] for c in settings.CROSSMATCH_CATALOGS]
    assert schema['catalog_match']['enum'] == ['with', 'without']
    assert schema['top_n']['maximum'] == 7
    assert schema['last_nights']['maximum'] == 12
    description = tools[SUMMARY_TOOL]['description']
    for phrase in ('last_nights=7', 'gaia_dr3', 'skymapper_dr4',
                   'Pitt-Google', 'lookup_rubin_transients', 'time zone'):
        assert phrase in description


@pytest.mark.django_db(transaction=True)
def test_summary_with_no_arguments_answers_the_default_night(client, frozen_now):
    ids = _summary_objects()

    result = call_tool(client, SUMMARY_TOOL, {})

    assert result['isError'] is False
    got = answer(result)
    assert got['window']['form'] == 'default'
    assert got['window']['nights'] == ['2026-11-09']
    assert got['objects_total'] == 3
    assert [t['diaObjectId'] for t in got['top']] == [str(i) for i in ids]
    for entry in got['top']:
        assert isinstance(entry['diaObjectId'], str)
        assert entry['links']['lasair'] and entry['links']['antares']


@pytest.mark.django_db(transaction=True)
def test_summary_tool_counts_equal_the_service_body_and_its_request(client, frozen_now):
    from urllib.parse import urlsplit

    from api.summary import summarize_window

    _summary_objects(4)
    arguments = {'last_nights': 3, 'broker': 'Pitt-Google', 'top_n': 2}

    got = answer(call_tool(client, SUMMARY_TOOL, arguments))
    body = summarize_window(arguments, now=frozen_now)

    assert got['objects_total'] == body['totals']['objects'] == 4
    assert got['nights'] == body['totals']['nights']
    assert got['brokers'] == body['brokers']
    assert got['catalogs'] == body['catalogs']
    assert got['basis'] == body['basis']
    assert got['reliability']['no_score'] == body['reliability']['no_score']
    assert got['reliability']['histogram'] == [
        h['objects'] for h in body['reliability']['histogram']
    ]
    assert [t['diaObjectId'] for t in got['top']] == [
        t['diaObjectId_str'] for t in body['top']
    ]
    url = urlsplit(got['request']['url'])
    assert f'{url.path}?{url.query}' == body['request']
    assert got['filters'] == body['filters']


@pytest.mark.django_db
@pytest.mark.parametrize('arguments, limit', [
    ({'top_n': 21}, '20'),
    ({'last_nights': 60}, '14'),
    ({'start': '2026-09-01T00:00:00Z', 'end': '2026-11-01T00:00:00Z'}, '14'),
])
def test_summary_over_a_limit_is_an_error_naming_it(client, frozen_now, arguments, limit):
    result = call_tool(client, SUMMARY_TOOL, arguments)

    assert result['isError'] is True
    error = answer(result)['error']
    assert error['code'] == 'invalid_arguments'
    assert limit in error['message']
    assert 'Retry' in error['message']


def test_instructions_route_count_questions_to_the_summary_tool():
    from chat_mcp.views import INSTRUCTIONS

    assert SUMMARY_TOOL in INSTRUCTIONS
    assert 'last night' in INSTRUCTIONS


@pytest.mark.django_db(transaction=True)
def test_summary_call_logs_the_tool_counts_and_outcome(client, frozen_now):
    _summary_objects(3)

    with capture_logs() as logs:
        call_tool(client, SUMMARY_TOOL, {'top_n': 2})
        call_tool(client, SUMMARY_TOOL, {'top_n': 0})

    ok_line, invalid_line = tool_logs(logs)
    assert ok_line['tool'] == SUMMARY_TOOL
    assert ok_line['outcome'] == 'ok'
    assert ok_line['objects_total'] == 3
    assert ok_line['top_returned'] == 2
    assert ok_line['nights'] == 1
    assert ok_line['truncated'] is False
    assert invalid_line['tool'] == SUMMARY_TOOL
    assert invalid_line['outcome'] == 'invalid_arguments'
