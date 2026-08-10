"""Payload retention sweep + notified_at backfill.

Covers the sweep drop/keep matrix (AE1-AE5, AE8), the per-run row cap, and the U5
backfill of notified_at for terminal alerts only.
"""

import importlib
from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from core.models import Alert, Notification
from tasks.retention import sweep_payloads
from tests.factories import AlertFactory, NotificationFactory


def _alert(status, notified_at, payload=None):
    return AlertFactory(
        status=status,
        notified_at=notified_at,
        payload={"raw": "x"} if payload is None else payload,
    )


@pytest.mark.django_db
def test_sweep_clears_past_grace_alert_and_keeps_row():
    # AE1: notified_at past grace -> payload nulled; status/row unchanged.
    old = timezone.now() - timedelta(days=40)
    a = _alert(Alert.Status.NOTIFIED, old)
    result = sweep_payloads(grace_days=30, max_rows=1000)
    a.refresh_from_db()
    assert a.payload is None
    assert a.status == Alert.Status.NOTIFIED
    assert Alert.objects.filter(pk=a.pk).exists()
    assert result["alerts_cleared"] == 1


@pytest.mark.django_db
def test_sweep_retains_within_grace():
    # AE2: notified_at within grace -> retained.
    a = _alert(Alert.Status.NOTIFIED, timezone.now() - timedelta(days=5))
    sweep_payloads(grace_days=30, max_rows=1000)
    a.refresh_from_db()
    assert a.payload is not None


@pytest.mark.django_db
def test_sweep_retains_in_flight_regardless_of_age():
    # AE3: in-flight alert (notified_at NULL) -> retained regardless of grace.
    a = _alert(Alert.Status.MATCHED, None)
    sweep_payloads(grace_days=0, max_rows=1000)
    a.refresh_from_db()
    assert a.payload is not None


@pytest.mark.django_db
def test_sweep_clears_no_match_alert_past_grace():
    # AE8: no-match alert (MATCHED, notified_at set at completion) past grace.
    old = timezone.now() - timedelta(days=40)
    a = _alert(Alert.Status.MATCHED, old)
    sweep_payloads(grace_days=30, max_rows=1000)
    a.refresh_from_db()
    assert a.payload is None


@pytest.mark.django_db
def test_sweep_notifications_anchor_on_sent_at():
    # AE4: SENT past grace -> cleared; PENDING/FAILED (sent_at NULL) -> retained.
    old = timezone.now() - timedelta(days=40)
    alert = AlertFactory(status=Alert.Status.NOTIFIED, notified_at=old)
    sent = NotificationFactory(
        alert=alert, state=Notification.State.SENT, sent_at=old, payload={"p": 1}
    )
    pending = NotificationFactory(
        alert=alert, state=Notification.State.PENDING, sent_at=None, payload={"p": 2}
    )
    failed = NotificationFactory(
        alert=alert, state=Notification.State.FAILED, sent_at=None, payload={"p": 3}
    )
    result = sweep_payloads(grace_days=30, max_rows=1000)
    sent.refresh_from_db()
    pending.refresh_from_db()
    failed.refresh_from_db()
    assert sent.payload is None
    assert pending.payload is not None
    assert failed.payload is not None
    assert result["notifications_cleared"] == 1


@pytest.mark.django_db
def test_sweep_is_idempotent():
    # AE5: a second run over already-cleared rows makes no changes.
    old = timezone.now() - timedelta(days=40)
    _alert(Alert.Status.NOTIFIED, old)
    first = sweep_payloads(grace_days=30, max_rows=1000)
    second = sweep_payloads(grace_days=30, max_rows=1000)
    assert first["alerts_cleared"] == 1
    assert second["alerts_cleared"] == 0


@pytest.mark.django_db
def test_sweep_row_cap_bounds_work_per_run():
    old = timezone.now() - timedelta(days=40)
    for _ in range(5):
        _alert(Alert.Status.NOTIFIED, old)
    result = sweep_payloads(grace_days=30, max_rows=2)
    assert result["alerts_cleared"] == 2
    assert Alert.objects.filter(payload__isnull=False).count() == 3


@pytest.mark.django_db
def test_backfill_sets_notified_at_for_terminal_alerts_only():
    # U5: NOTIFIED and no-match MATCHED backfilled; in-flight left NULL.
    mod = importlib.import_module("core.migrations.0008_backfill_notified_at")
    notified = AlertFactory(status=Alert.Status.NOTIFIED, notified_at=None)
    no_match = AlertFactory(status=Alert.Status.MATCHED, notified_at=None)
    in_flight_matched = AlertFactory(status=Alert.Status.MATCHED, notified_at=None)
    NotificationFactory(
        alert=in_flight_matched, state=Notification.State.PENDING
    )
    queued = AlertFactory(status=Alert.Status.QUEUED, notified_at=None)

    mod.backfill_notified_at(django_apps, None)

    for a in (notified, no_match, in_flight_matched, queued):
        a.refresh_from_db()
    assert notified.notified_at is not None
    assert no_match.notified_at is not None
    assert in_flight_matched.notified_at is None
    assert queued.notified_at is None


@pytest.mark.django_db
def test_backfill_covers_matched_with_all_notifications_sent():
    # A MATCHED alert whose notifications are all SENT is terminal (no unsent) and
    # must be backfilled, not left NULL.
    mod = importlib.import_module("core.migrations.0008_backfill_notified_at")
    alert = AlertFactory(status=Alert.Status.MATCHED, notified_at=None)
    NotificationFactory(
        alert=alert, state=Notification.State.SENT, sent_at=timezone.now()
    )
    mod.backfill_notified_at(django_apps, None)
    alert.refresh_from_db()
    assert alert.notified_at is not None


@pytest.mark.django_db
def test_backfill_pk_cursor_covers_all_batches(monkeypatch):
    # With more terminal rows than one batch, the monotonic pk-cursor must visit
    # every window, anchor all terminal rows, leave in-flight rows NULL, and
    # terminate (no infinite loop, no missed rows).
    mod = importlib.import_module("core.migrations.0008_backfill_notified_at")
    monkeypatch.setattr(mod, "BATCH_SIZE", 3)
    terminal = [
        AlertFactory(status=Alert.Status.NOTIFIED, notified_at=None) for _ in range(7)
    ]
    queued = AlertFactory(status=Alert.Status.QUEUED, notified_at=None)
    matched_pending = AlertFactory(status=Alert.Status.MATCHED, notified_at=None)
    NotificationFactory(alert=matched_pending, state=Notification.State.PENDING)

    mod.backfill_notified_at(django_apps, None)

    for a in terminal:
        a.refresh_from_db()
        assert a.notified_at is not None
    queued.refresh_from_db()
    matched_pending.refresh_from_db()
    assert queued.notified_at is None
    assert matched_pending.notified_at is None
