"""U7: rate limits and the concurrency cap on MCP tool calls (R12, R13; KTD4; AE6).

Callers from the chat-provider ranges are limited per session ID, and those
with no valid session share one per-provider bucket; everyone else is limited
per client IP. In-flight tool calls are capped cluster-wide and per limiter
key. An over-limit call is an ``isError`` tool result with a retry-after, never
an HTTP 429. Every test runs on its own locmem cache, because the documented
test run has no Valkey.
"""

import dataclasses
import json
import time
import uuid

import pytest
from django.core.cache import cache
from structlog.testing import capture_logs

from chat_mcp import limits, protocol
from chat_mcp.tools import TOOLS

MCP_URL = '/mcp'
DESCRIBE = 'describe_crossmatch_service'
PROVIDER_IP = '160.79.104.10'
OTHER_PROVIDER_IP = '160.79.111.200'
DIRECT_IP = '203.0.113.5'
#: A frozen clock on a window boundary for every rate in these tests.
T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def locmem_cache(settings):
    settings.CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': f'mcp-limits-{uuid.uuid4()}',
    }}
    yield
    cache.clear()


@pytest.fixture
def clock(monkeypatch):
    """The limiter's clock, frozen at ``T0``; set ``clock.now`` to move it."""
    state = type('Clock', (), {'now': T0})()
    monkeypatch.setattr(limits, '_now', lambda: state.now)
    return state


def call(client, *, ip=None, remote_addr='127.0.0.1', session_id=None):
    """POST one describe tools/call; return (HTTP response, tool result)."""
    body = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': DESCRIBE, 'arguments': {}},
    }
    headers = {}
    if ip is not None:
        headers['X-Real-Ip'] = ip
    if session_id is not None:
        headers['Mcp-Session-Id'] = session_id
    resp = client.post(
        MCP_URL, json.dumps(body), content_type='application/json',
        headers=headers, REMOTE_ADDR=remote_addr,
    )
    return resp, resp.json()['result']


def error_of(result):
    [block] = result['content']
    return json.loads(block['text'])['error']


def exhaust(client, n, **kwargs):
    for _ in range(n):
        resp, result = call(client, **kwargs)
        assert resp.status_code == 200
        assert result['isError'] is False, result


def assert_limited(client, code='rate_limited', **kwargs):
    resp, result = call(client, **kwargs)
    assert resp.status_code == 200
    assert result['isError'] is True
    error = error_of(result)
    assert error['code'] == code
    assert error['retryable'] is True
    assert isinstance(error['retry_after_seconds'], int)
    assert error['retry_after_seconds'] >= 1
    return error


def caller_key(ip, session_id=None):
    token = protocol.verify_session_id(session_id) if session_id else None
    return limits.identify_caller(ip, token).key


# --- rate limits (KTD4) ---

@pytest.mark.django_db
def test_ae6_two_provider_sessions_each_get_their_own_allowance(client, clock):
    first, second = protocol.issue_session_id(), protocol.issue_session_id()

    exhaust(client, 10, ip=PROVIDER_IP, session_id=first)
    assert_limited(client, ip=PROVIDER_IP, session_id=first)

    exhaust(client, 10, ip=PROVIDER_IP, session_id=second)


@pytest.mark.django_db
def test_an_over_limit_call_is_an_iserror_result_with_retry_after_not_a_429(
    client, clock,
):
    exhaust(client, 10, ip=DIRECT_IP)

    error = assert_limited(client, ip=DIRECT_IP)

    assert '2 per second' in error['message']


@pytest.mark.django_db
def test_a_direct_caller_is_limited_at_2_per_second_burst_10_by_ip(client, clock):
    exhaust(client, 10, ip=DIRECT_IP)
    assert_limited(client, ip=DIRECT_IP)

    # The provider session is on its own key.
    exhaust(client, 1, ip=PROVIDER_IP, session_id=protocol.issue_session_id())

    # The allowance refills at 2 per second once the burst window has passed.
    clock.now = T0 + 5.5
    exhaust(client, 1, ip=DIRECT_IP)
    assert_limited(client, ip=DIRECT_IP)
    clock.now = T0 + 15.0
    exhaust(client, 10, ip=DIRECT_IP)


@pytest.mark.django_db
def test_a_direct_caller_minting_new_sessions_is_still_limited_by_ip(client, clock):
    for _ in range(10):
        exhaust(client, 1, ip=DIRECT_IP, session_id=protocol.issue_session_id())

    assert_limited(client, ip=DIRECT_IP, session_id=protocol.issue_session_id())


@pytest.mark.django_db
def test_sessionless_provider_calls_share_one_bucket_apart_from_sessions(
    client, clock, settings,
):
    settings.MCP_PROVIDER_RATE = (1.0, 3)

    exhaust(client, 2, ip=PROVIDER_IP)
    exhaust(client, 1, ip=OTHER_PROVIDER_IP)
    assert_limited(client, ip=PROVIDER_IP)
    assert_limited(client, ip=OTHER_PROVIDER_IP, session_id='not-a-valid-session')

    exhaust(client, 10, ip=PROVIDER_IP, session_id=protocol.issue_session_id())


@pytest.mark.django_db
def test_a_missing_x_real_ip_falls_back_to_remote_addr(client, clock):
    with capture_logs() as logs:
        call(client, remote_addr=PROVIDER_IP)
        call(client, ip=DIRECT_IP, remote_addr=PROVIDER_IP)

    classes = [e['client_class'] for e in logs if e['event'] == 'mcp tool call']
    assert classes == ['provider', 'direct']


# --- concurrency cap (KTD4, R13) ---

def global_slots():
    return [cache.get(f'mcp:slot:{i}') for i in range(6)]


@pytest.mark.django_db
def test_the_seventh_concurrent_call_is_busy_and_every_slot_frees(
    client, clock, monkeypatch,
):
    held = [limits.claim_slot('mcp:slot', 6, f'held-{i}', 60) for i in range(6)]
    assert all(held)

    error = assert_limited(client, code='busy', ip=DIRECT_IP)
    assert 'retry' in error['message']

    for key, token in zip(held, (f'held-{i}' for i in range(6))):
        limits.release_slot(key, token)
    exhaust(client, 1, ip=DIRECT_IP)

    tool = TOOLS[DESCRIBE]

    def boom(guard, parsed):
        raise RuntimeError('tool crashed')

    monkeypatch.setitem(TOOLS, DESCRIBE, dataclasses.replace(tool, run=boom))
    with pytest.raises(RuntimeError):
        call(client, ip=DIRECT_IP)

    assert global_slots() == [None] * 6
    key = caller_key(DIRECT_IP)
    assert [cache.get(f'mcp:keyslot:{key}:{i}') for i in range(2)] == [None, None]


@pytest.mark.django_db
def test_a_slot_never_released_frees_itself_at_timeout(client, settings, monkeypatch):
    settings.API_REQUEST_BUDGET_SECONDS = 5
    timeout = limits.slot_timeout()
    real_time = time.time
    start = real_time()

    monkeypatch.setattr(time, 'time', lambda: start)
    assert limits.claim_slot('mcp:slot', 6, 'killed-worker', timeout)
    monkeypatch.setattr(time, 'time', lambda: start + timeout - 1)
    others = [limits.claim_slot('mcp:slot', 6, f'live-{i}', timeout) for i in range(5)]
    assert all(others)
    assert_limited(client, code='busy', ip=DIRECT_IP)

    monkeypatch.setattr(time, 'time', lambda: start + timeout + 1)
    exhaust(client, 1, ip=DIRECT_IP)

    assert [cache.get(key) for key in others] == [f'live-{i}' for i in range(5)]


@pytest.mark.django_db
def test_one_session_with_two_calls_in_flight_is_busy_others_still_answered(
    client, clock,
):
    busy_session, other_session = protocol.issue_session_id(), protocol.issue_session_id()
    key = caller_key(PROVIDER_IP, busy_session)
    for i in range(2):
        assert limits.claim_slot(f'mcp:keyslot:{key}', 2, f'in-flight-{i}', 60)

    error = assert_limited(client, code='busy', ip=PROVIDER_IP, session_id=busy_session)
    assert '2 tool calls in progress' in error['message']

    exhaust(client, 1, ip=PROVIDER_IP, session_id=other_session)


def test_a_slot_is_released_only_by_its_own_holder():
    key = limits.claim_slot('mcp:slot', 1, 'mine', 60)

    limits.release_slot(key, 'someone-else')
    assert cache.get(key) == 'mine'
    limits.release_slot(key, 'mine')
    assert cache.get(key) is None


# --- cache unreachable ---

class _BrokenCache:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionError('valkey unreachable')
        return fail


@pytest.mark.django_db
def test_an_unreachable_cache_fails_open_with_a_warning(client, monkeypatch):
    monkeypatch.setattr(limits, 'cache', _BrokenCache())

    with capture_logs() as logs:
        for _ in range(12):
            resp, result = call(client, ip=DIRECT_IP)
            assert result['isError'] is False

    warnings = [e for e in logs if e['event'] == 'mcp limiter cache unavailable']
    assert warnings and all(e['log_level'] == 'warning' for e in warnings)
