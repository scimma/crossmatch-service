"""Tests for the API request guard (U3, KTD12): the per-request budget, the
read-only transaction, deadline-first error classification, and phase logging.

The guard is exercised end to end through throwaway views routed by this
module's own ``urlpatterns`` (``ROOT_URLCONF='tests.test_api_guard'``), so no
production URL exists only for tests. Overrun tests cancel statements on the
test connection, so they use ``transaction=True``.
"""

import time

import pytest
from django.db import DatabaseError, OperationalError, connection
from django.http import HttpRequest, JsonResponse
from django.test import override_settings
from django.urls import include, path
from structlog.testing import capture_logs

from api.guard import api_guard, check_deadline, sql_phase
from core.models import Alert
from tests.factories import AlertFactory

_SECRET_HOST = 'db-secret-host.internal'


@api_guard
def _show_timeout_view(request: HttpRequest) -> JsonResponse:
    with sql_phase():
        with connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            return JsonResponse({'statement_timeout': cursor.fetchone()[0]})


@api_guard
def _show_timeout_twice_view(request: HttpRequest) -> JsonResponse:
    shown = []
    for _ in range(2):
        with sql_phase():
            with connection.cursor() as cursor:
                cursor.execute('SHOW statement_timeout')
                shown.append(cursor.fetchone()[0])
        time.sleep(0.3)
    return JsonResponse({'shown': shown})


@api_guard
def _write_view(request: HttpRequest) -> JsonResponse:
    with sql_phase():
        AlertFactory()
    return JsonResponse({'ok': True})


@api_guard
def _many_fast_statements_view(request: HttpRequest) -> JsonResponse:
    for _ in range(50):
        with sql_phase():
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_sleep(0.02)')
    return JsonResponse({'ok': True})


@api_guard
def _one_long_statement_view(request: HttpRequest) -> JsonResponse:
    with sql_phase():
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_sleep(0.3)')
    with sql_phase():
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_sleep(10)')
    return JsonResponse({'ok': True})


@api_guard
def _slow_python_view(request: HttpRequest) -> JsonResponse:
    time.sleep(0.4)
    return JsonResponse({'ok': True})


@api_guard
def _python_loop_view(request: HttpRequest) -> JsonResponse:
    for _ in range(200):
        check_deadline()
        time.sleep(0.01)
    return JsonResponse({'ok': True})


@api_guard
def _connection_lost_view(request: HttpRequest) -> JsonResponse:
    raise OperationalError(
        f'connection to server at "{_SECRET_HOST}" (10.1.2.3), port 5432 failed'
    )


@api_guard
def _connection_lost_after_deadline_view(request: HttpRequest) -> JsonResponse:
    time.sleep(0.3)
    raise OperationalError('server closed the connection unexpectedly')


@api_guard(map_unavailable=False)
def _unmapped_view(request: HttpRequest) -> JsonResponse:
    raise OperationalError('server closed the connection unexpectedly')


@api_guard
def _ok_view(request: HttpRequest) -> JsonResponse:
    with sql_phase():
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    return JsonResponse({'ok': True, 'pad': 'x' * 100})


urlpatterns = [
    path('show-timeout', _show_timeout_view),
    path('show-timeout-twice', _show_timeout_twice_view),
    path('write', _write_view),
    path('many-fast', _many_fast_statements_view),
    path('one-long', _one_long_statement_view),
    path('slow-python', _slow_python_view),
    path('python-loop', _python_loop_view),
    path('connection-lost', _connection_lost_view),
    path('connection-lost-late', _connection_lost_after_deadline_view),
    path('unmapped', _unmapped_view),
    path('ok', _ok_view),
    # The real site too, so the 500 handler's templates can reverse web: URLs.
    path('', include('project.urls')),
]

guard_urls = override_settings(ROOT_URLCONF='tests.test_api_guard')


def _parse_ms(value: str) -> float:
    """Parse a Postgres ``SHOW statement_timeout`` value into milliseconds."""
    for unit, scale in (('ms', 1), ('s', 1000), ('min', 60000)):
        if value.endswith(unit) and value[: -len(unit)].isdigit():
            return int(value[: -len(unit)]) * scale
    raise AssertionError(f'unexpected statement_timeout {value!r}')


def _assert_too_expensive(resp) -> None:
    assert resp.status_code == 400
    assert resp['Content-Type'] == 'application/json'
    body = resp.json()
    assert body['code'] == 'query_too_expensive'
    assert body['retryable'] is False
    assert body['error'] == body['message']
    assert 'Retry-After' not in resp


@pytest.mark.django_db(transaction=True)
@guard_urls
@override_settings(API_REQUEST_BUDGET_SECONDS=2)
def test_guard_sets_statement_timeout_to_the_remaining_budget(client):
    resp = client.get('/show-timeout')

    assert resp.status_code == 200
    ms = _parse_ms(resp.json()['statement_timeout'])
    assert 0 < ms <= 2000


@pytest.mark.django_db(transaction=True)
@guard_urls
@override_settings(API_REQUEST_BUDGET_SECONDS=2)
def test_each_sql_phase_resets_the_timeout_to_what_is_left(client):
    resp = client.get('/show-timeout-twice')

    assert resp.status_code == 200
    first, second = (_parse_ms(v) for v in resp.json()['shown'])
    assert 0 < second <= first - 250


@pytest.mark.django_db(transaction=True)
@guard_urls
def test_guard_runs_the_view_read_only(client):
    with pytest.raises(DatabaseError):
        client.get('/write')
    assert Alert.objects.count() == 0


@pytest.mark.django_db(transaction=True)
@guard_urls
@override_settings(API_REQUEST_BUDGET_SECONDS=0.3)
def test_many_fast_statements_over_the_budget_are_too_expensive(client):
    started = time.monotonic()
    resp = client.get('/many-fast')

    _assert_too_expensive(resp)
    assert time.monotonic() - started < 1.0


@pytest.mark.django_db(transaction=True)
@guard_urls
@override_settings(API_REQUEST_BUDGET_SECONDS=0.5)
def test_a_statement_is_cancelled_at_the_remaining_budget(client):
    # The second statement would run 10 s; its timeout is reset to what is left
    # of the 0.5 s budget, so Postgres cancels it (SQLSTATE 57014) quickly.
    started = time.monotonic()
    resp = client.get('/one-long')

    _assert_too_expensive(resp)
    assert time.monotonic() - started < 2.0


@pytest.mark.django_db(transaction=True)
@guard_urls
@override_settings(API_REQUEST_BUDGET_SECONDS=0.2)
def test_a_python_phase_past_the_deadline_is_too_expensive(client):
    _assert_too_expensive(client.get('/slow-python'))


@pytest.mark.django_db(transaction=True)
@guard_urls
@override_settings(API_REQUEST_BUDGET_SECONDS=0.2)
def test_check_deadline_stops_a_python_loop(client):
    started = time.monotonic()
    resp = client.get('/python-loop')

    _assert_too_expensive(resp)
    assert time.monotonic() - started < 1.0


@pytest.mark.django_db
@guard_urls
def test_connection_loss_is_a_structured_503(client):
    with capture_logs() as logs:
        resp = client.get('/connection-lost')

    assert resp.status_code == 503
    assert resp['Content-Type'] == 'application/json'
    assert int(resp['Retry-After']) > 0
    body = resp.json()
    assert body['code'] == 'service_unavailable'
    assert body['retryable'] is True
    assert body['error'] == body['message']
    # The raw exception names the database host; it goes to the log, not the body.
    assert _SECRET_HOST.encode() not in resp.content
    assert any(_SECRET_HOST in str(entry.get('error', '')) for entry in logs)


@pytest.mark.django_db
@guard_urls
@override_settings(API_REQUEST_BUDGET_SECONDS=0.1)
def test_a_database_error_after_the_deadline_is_too_expensive(client):
    _assert_too_expensive(client.get('/connection-lost-late'))


@pytest.mark.django_db
@guard_urls
def test_a_view_can_opt_out_of_the_503_mapping(client):
    with pytest.raises(OperationalError):
        client.get('/unmapped')


@pytest.mark.django_db
@guard_urls
def test_phase_timings_are_logged(client):
    with capture_logs() as logs:
        resp = client.get('/ok')

    assert resp.status_code == 200
    (entry,) = [e for e in logs if e['event'] == 'api request']
    assert entry['status'] == 200
    assert entry['response_bytes'] == len(resp.content)
    assert entry['sql_phases'] == 1
    for key in ('sql_seconds', 'python_seconds', 'total_seconds'):
        assert entry[key] >= 0


def test_sql_phase_and_check_deadline_are_no_ops_outside_a_guard():
    check_deadline()
    with sql_phase():
        pass
