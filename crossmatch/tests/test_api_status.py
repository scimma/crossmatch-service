"""U7 / R30, AE5; KTD16: ``GET api/status``.

The status endpoint always answers 200: it reports ``database: ok|unavailable``
from a short-timeout check that never raises (it is exempt from the guard's 503
mapping, KTD12 step 6), the service and contract versions, and whether TNS-name
resolution is available, judged by the same snapshot-currency helper the
resolver uses. ``/healthz`` (the pod probe) is left unchanged.
"""

import time
from datetime import timedelta

import pytest
from django.db import OperationalError, connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from api import discovery
from core.models import TnsSnapshotMeta
from core.provenance import CONTRACT_VERSION

STATUS = '/api/status'


def test_status_route_is_slashless():
    assert reverse('service-status') == STATUS


@pytest.mark.django_db
@override_settings(APP_VERSION='9.8.7')
def test_database_ok_with_current_snapshot(client, openapi_validate):
    meta = TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())

    resp = client.get(STATUS)

    assert resp.status_code == 200
    body = resp.json()
    assert body['database'] == 'ok'
    assert body['service_version'] == '9.8.7'
    assert body['contract_version'] == CONTRACT_VERSION
    assert body['tns_resolution'] == 'available'
    assert body['tns_snapshot_epoch'] == meta.last_refresh_epoch.isoformat()
    assert body['provenance']['service_version'] == '9.8.7'
    openapi_validate('service_status', 200, body)


@pytest.mark.django_db
def test_no_snapshot_means_tns_unavailable(client, openapi_validate):
    resp = client.get(STATUS)

    assert resp.status_code == 200
    body = resp.json()
    assert body['database'] == 'ok'
    assert body['tns_resolution'] == 'unavailable'
    assert body['tns_snapshot_epoch'] is None
    openapi_validate('service_status', 200, body)


@pytest.mark.django_db
@override_settings(TNS_SNAPSHOT_MAX_AGE_SECONDS=3600)
def test_stale_snapshot_means_tns_unavailable(client, openapi_validate):
    """AE5: the snapshot refresh is not running, so resolution is unavailable."""
    TnsSnapshotMeta.objects.create(
        pk=1, last_refresh_epoch=timezone.now() - timedelta(days=2)
    )

    resp = client.get(STATUS)

    assert resp.status_code == 200
    body = resp.json()
    assert body['database'] == 'ok'
    assert body['tns_resolution'] == 'unavailable'
    assert body['tns_snapshot_epoch'] is None
    openapi_validate('service_status', 200, body)


@pytest.mark.django_db(transaction=True)
def test_unreachable_database_still_200_within_connect_timeout(client, openapi_validate):
    """Point the connection at an unroutable address (TEST-NET-1) and reconnect.

    The real database is untouched; only this connection's settings change, and
    they are restored before teardown reconnects to flush. (``openapi_validate``
    fetched the document during setup, while the connection was still good.)
    """
    connect_timeout = 2
    saved_host = connection.settings_dict['HOST']
    saved_options = connection.settings_dict.get('OPTIONS', {})
    connection.close()
    connection.settings_dict['HOST'] = '192.0.2.1'
    connection.settings_dict['OPTIONS'] = {**saved_options, 'connect_timeout': connect_timeout}
    try:
        started = time.monotonic()
        resp = client.get(STATUS)
        elapsed = time.monotonic() - started
    finally:
        connection.close()
        connection.settings_dict['HOST'] = saved_host
        connection.settings_dict['OPTIONS'] = saved_options

    assert resp.status_code == 200
    body = resp.json()
    assert body['database'] == 'unavailable'
    assert body['tns_resolution'] == 'unavailable'
    assert body['tns_snapshot_epoch'] is None
    assert body['contract_version'] == CONTRACT_VERSION
    assert elapsed < connect_timeout + 2
    openapi_validate('service_status', 200, body)


@pytest.mark.django_db
def test_database_error_during_check_is_reported_not_raised(
    client, openapi_validate, monkeypatch,
):
    def _lost(*args, **kwargs):
        raise OperationalError('server closed the connection unexpectedly')

    monkeypatch.setattr(TnsSnapshotMeta, 'current_epoch', classmethod(_lost))

    resp = client.get(STATUS)

    assert resp.status_code == 200
    body = resp.json()
    assert body['database'] == 'unavailable'
    assert body['tns_resolution'] == 'unavailable'
    assert 'Retry-After' not in resp.headers
    openapi_validate('service_status', 200, body)


@pytest.mark.django_db
def test_slow_database_is_unavailable_not_query_too_expensive(
    client, openapi_validate, monkeypatch,
):
    """A check that outruns the short status timeout reports unavailable, not 400."""
    monkeypatch.setattr(discovery, 'STATUS_CHECK_TIMEOUT_SECONDS', 0.05)

    def _slow(*args, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_sleep(1)')

    monkeypatch.setattr(TnsSnapshotMeta, 'current_epoch', classmethod(_slow))

    resp = client.get(STATUS)

    assert resp.status_code == 200
    assert resp.json()['database'] == 'unavailable'
    openapi_validate('service_status', 200, resp.json())


def test_status_rejects_non_get_with_structured_error(client, openapi_validate):
    resp = client.post(STATUS)

    assert resp.status_code == 405
    body = resp.json()
    assert body['code'] == 'method_not_allowed'
    openapi_validate('service_status', 405, body)


def test_healthz_is_unchanged(client):
    resp = client.get('/healthz')

    assert resp.status_code == 200
    assert resp.json() == {'status': 'ok'}
