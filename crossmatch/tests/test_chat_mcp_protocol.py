"""U6: the MCP endpoint's protocol surface (KTD1-KTD3, KTD13; F2).

``POST /mcp`` speaks JSON-RPC 2.0 with plain ``application/json`` responses:
version negotiation, the optional signed session ID, notifications, the
transport's HTTP status cases, the Origin guard, and the JSON-RPC errors
reserved for protocol faults.
"""

import json
import time
import uuid

import pytest
from django.core import signing
from django.test import override_settings

from chat_mcp import protocol

MCP_URL = '/mcp'


@pytest.fixture(autouse=True)
def locmem_cache(settings):
    """A fresh locmem cache per test: tools/call touches the rate limiter's
    cache (U7), and the documented test run has no Valkey."""
    settings.CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': f'mcp-{uuid.uuid4()}',
    }}
TOOL_NAMES = {
    'lookup_rubin_transients',
    'search_rubin_transients_near_position',
    'describe_crossmatch_service',
}
READ_ONLY_ANNOTATIONS = {
    'readOnlyHint': True,
    'destructiveHint': False,
    'idempotentHint': True,
    'openWorldHint': False,
}


def post(client, body, **headers):
    payload = body if isinstance(body, str) else json.dumps(body)
    return client.post(MCP_URL, payload, content_type='application/json', headers=headers)


def rpc(client, method, params=None, *, msg_id=1, **headers):
    body = {'jsonrpc': '2.0', 'id': msg_id, 'method': method}
    if params is not None:
        body['params'] = params
    return post(client, body, **headers)


def initialize(client, version='2025-06-18'):
    return rpc(client, 'initialize', {
        'protocolVersion': version,
        'capabilities': {},
        'clientInfo': {'name': 'test-client', 'version': '1.0'},
    })


def describe_call(client, **headers):
    return rpc(client, 'tools/call', {
        'name': 'describe_crossmatch_service', 'arguments': {},
    }, **headers)


# --- initialize (F2) ---

@pytest.mark.django_db
def test_initialize_with_2025_06_18_echoes_it(client):
    resp = initialize(client, '2025-06-18')

    assert resp.status_code == 200
    assert resp['Content-Type'] == 'application/json'
    body = resp.json()
    assert body['jsonrpc'] == '2.0' and body['id'] == 1
    result = body['result']
    assert result['protocolVersion'] == '2025-06-18'
    assert result['capabilities'] == {'tools': {'listChanged': False}}
    assert result['serverInfo']['name']
    assert result['serverInfo']['version']
    assert isinstance(result['instructions'], str) and result['instructions']
    session_id = resp['Mcp-Session-Id']
    assert session_id
    assert protocol.verify_session_id(session_id) is not None


@pytest.mark.django_db
def test_each_initialize_issues_a_different_session(client):
    first = initialize(client)['Mcp-Session-Id']
    second = initialize(client)['Mcp-Session-Id']
    assert first != second


@pytest.mark.django_db
@pytest.mark.parametrize('version', ['2025-03-26', '2025-11-25'])
def test_initialize_echoes_every_supported_version(client, version):
    assert initialize(client, version).json()['result']['protocolVersion'] == version


@pytest.mark.django_db
def test_initialize_with_an_unknown_future_version_answers_2025_11_25(client):
    resp = initialize(client, '2026-07-28')
    assert resp.json()['result']['protocolVersion'] == '2025-11-25'


# --- notifications and ping ---

@pytest.mark.django_db
def test_notifications_initialized_returns_202_with_no_body(client):
    resp = post(client, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert resp.status_code == 202
    assert resp.content == b''


@pytest.mark.django_db
def test_any_notification_returns_202_with_no_body(client):
    resp = post(client, {
        'jsonrpc': '2.0', 'method': 'notifications/cancelled',
        'params': {'requestId': 3},
    })
    assert resp.status_code == 202
    assert resp.content == b''


@pytest.mark.django_db
def test_ping_returns_an_empty_result(client):
    resp = rpc(client, 'ping', msg_id='p-1')
    assert resp.status_code == 200
    assert resp.json() == {'jsonrpc': '2.0', 'id': 'p-1', 'result': {}}


# --- tools/list ---

@pytest.mark.django_db
def test_tools_list_returns_three_tools_with_titles_annotations_and_schemas(client):
    resp = rpc(client, 'tools/list')

    assert resp.status_code == 200
    tools = resp.json()['result']['tools']
    assert {tool['name'] for tool in tools} == TOOL_NAMES
    for tool in tools:
        assert tool['title']
        assert tool['description']
        assert tool['annotations'] == READ_ONLY_ANNOTATIONS
        assert tool['inputSchema']['type'] == 'object'
        assert 'outputSchema' not in tool


# --- sessions are optional (KTD3) ---

def _expired_session_id(monkeypatch):
    issued_at = time.time() - 10 * 86400
    with monkeypatch.context() as patch:
        patch.setattr(signing.time, 'time', lambda: issued_at)
        return protocol.issue_session_id()


@pytest.mark.django_db
@pytest.mark.parametrize('session', ['none', 'tampered', 'expired'])
def test_tools_call_without_a_valid_session_or_initialize_is_answered(
    client, monkeypatch, session,
):
    headers = {}
    if session == 'tampered':
        headers['Mcp-Session-Id'] = protocol.issue_session_id()[:-2] + 'xx'
    elif session == 'expired':
        headers['Mcp-Session-Id'] = _expired_session_id(monkeypatch)
    if 'Mcp-Session-Id' in headers:
        assert protocol.verify_session_id(headers['Mcp-Session-Id']) is None

    resp = describe_call(client, **headers)

    assert resp.status_code == 200
    result = resp.json()['result']
    assert result['isError'] is False
    assert result['content'][0]['type'] == 'text'


@pytest.mark.django_db
def test_a_valid_session_id_verifies_to_its_token(client):
    session_id = initialize(client)['Mcp-Session-Id']
    token = protocol.verify_session_id(session_id)
    assert token and token not in ('', session_id)


# --- transport HTTP cases ---

@pytest.mark.django_db
@pytest.mark.parametrize('method', ['get', 'delete'])
def test_get_and_delete_return_405(client, method):
    resp = getattr(client, method)(MCP_URL)
    assert resp.status_code == 405
    assert resp['Allow'] == 'POST'


@pytest.mark.django_db
def test_a_json_array_batch_body_returns_400(client):
    resp = post(client, [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
    ])
    assert resp.status_code == 400
    body = resp.json()
    assert body['error']['code'] == protocol.INVALID_REQUEST
    assert body['id'] is None


@pytest.mark.django_db
@pytest.mark.parametrize('raw', ['{"jsonrpc": "2.0", "id": 1,', '{"x": NaN}'])
def test_unparseable_json_is_a_parse_error(client, raw):
    resp = post(client, raw)
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == protocol.PARSE_ERROR


@pytest.mark.django_db
@pytest.mark.parametrize('body', [
    {'id': 1, 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': 1},
    {'jsonrpc': '2.0', 'id': True, 'method': 'ping'},
    'just a string',
])
def test_a_malformed_message_is_an_invalid_request(client, body):
    resp = post(client, json.dumps(body))
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == protocol.INVALID_REQUEST


@pytest.mark.django_db
def test_an_unknown_method_is_method_not_found(client):
    resp = rpc(client, 'resources/list', msg_id=7)
    assert resp.status_code == 200
    body = resp.json()
    assert body['id'] == 7
    assert body['error']['code'] == protocol.METHOD_NOT_FOUND
    assert 'result' not in body


@pytest.mark.django_db
def test_an_unknown_tool_is_a_jsonrpc_error_not_a_tool_result(client):
    resp = rpc(client, 'tools/call', {'name': 'delete_everything', 'arguments': {}})
    assert resp.status_code == 200
    body = resp.json()
    assert 'result' not in body
    assert body['error']['code'] == protocol.INVALID_PARAMS
    assert 'delete_everything' in body['error']['message']


# --- Origin guard (KTD13) ---

@pytest.mark.django_db
def test_a_disallowed_origin_returns_403(client):
    resp = rpc(client, 'ping', Origin='https://evil.example')
    assert resp.status_code == 403


@pytest.mark.django_db
@override_settings(MCP_ALLOWED_ORIGINS=('https://claude.ai',))
def test_an_allowed_origin_passes(client):
    assert rpc(client, 'ping', Origin='https://claude.ai').status_code == 200
    assert rpc(client, 'ping', Origin='https://evil.example').status_code == 403


@pytest.mark.django_db
def test_a_request_with_no_origin_succeeds(client):
    assert rpc(client, 'ping').status_code == 200


# --- no OAuth discovery (KTD13) ---

@pytest.mark.django_db
@pytest.mark.parametrize('path', [
    '/.well-known/oauth-protected-resource',
    '/.well-known/oauth-authorization-server',
])
def test_no_oauth_metadata_is_served(client, path):
    assert client.get(path).status_code == 404
