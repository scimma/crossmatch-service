"""Single-catalog resilience for the crossmatch batch.

When one catalog's read persistently fails (retries exhausted in
matching/catalog.py) but at least one other catalog succeeds, the batch no
longer aborts: it skips the failing catalog, finalizes the alert best-effort as
MATCHED (R1/R2), increments the skip counter for operators (R5), and stamps the
published payload so a consumer can tell the crossmatch was partial (R4). The
whole batch reverts only when EVERY catalog fails (R3) -- that all-fail guard is
also exercised here with two catalogs (the single-catalog case lives in
test_crossmatch_fail_loud.py). Only TRANSIENT reads skip; a deterministic error
(bad column, version skew) still fails loud even alongside a healthy catalog.

The Dask/LSDB path is mocked at its two seams (lsdb.from_dataframe and
crossmatch_alerts); crossmatch_alerts is dispatched per catalog so one catalog
can return matches while another raises.
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest
from django.test import override_settings

import tasks.crossmatch as crossmatch_mod
from core.metrics import CATALOG_SKIPS
from core.models import Alert, CatalogMatch, Notification, ObjectCrossmatchRecord
from tasks.crossmatch import crossmatch_batch
from tests.factories import AlertFactory

# Two catalogs so one can succeed while the other fails -- the resilience case
# that a single-catalog config cannot express (there, one failure is all-fail).
TWO_CATALOGS = [
    {
        "name": "cat_a",
        "release": "cat_a r1",
        "hats_url": "x",
        "source_id_column": "source_id",
        "ra_column": "ra",
        "dec_column": "dec",
        "payload_columns": ["mag"],
    },
    {
        "name": "cat_b",
        "release": "cat_b r1",
        "hats_url": "y",
        "source_id_column": "source_id",
        "ra_column": "ra",
        "dec_column": "dec",
        "payload_columns": ["mag"],
    },
]


def _counter(**labels):
    return CATALOG_SKIPS.labels(**labels)._value.get()


def _match_rows(*alerts):
    return pd.DataFrame(
        [
            {
                "lsst_diaObject_diaObjectId": alert.lsst_diaObject_diaObjectId,
                "source_id": f"cat-{i}",
                "_dist_arcsec": 0.4,
                "ra": 180.0,
                "dec": -30.0,
                "mag": 18.2,
            }
            for i, alert in enumerate(alerts)
        ]
    )


def _match_row(alert):
    return _match_rows(alert)


@pytest.fixture(autouse=True)
def _mock_lsdb(monkeypatch):
    monkeypatch.setattr(
        crossmatch_mod.lsdb, "from_dataframe", lambda *a, **k: MagicMock()
    )


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_one_catalog_skip_continues_and_marks_partial(monkeypatch):
    # R1/R2/R4/R5 / AE1: cat_a matches, cat_b's read fails after retries. The
    # batch skips cat_b and finalizes the alert MATCHED with cat_a's match; the
    # published payload is stamped partial with cat_b in catalogs_skipped, and
    # the skip counter for cat_b advances.
    alert = AlertFactory(status=Alert.Status.QUEUED)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "cat_a":
            return _match_row(alert)
        # The fsspec-wrapped transient form that survives retries in
        # matching/catalog.py -- a TypeError, not a RuntimeError, so it reaches
        # the skip arm (the RuntimeError arm is reserved for no-overlap).
        raise TypeError("can't concat ServerDisconnectedError to bytes")

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)
    skips_before = _counter(catalog="cat_b")

    crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.MATCHED  # not reverted; best-effort
    matches = CatalogMatch.objects.filter(alert=alert)
    assert matches.count() == 1
    assert matches.first().catalog_name == "cat_a"  # only the catalog that read

    notifications = Notification.objects.filter(alert=alert)
    assert notifications.count() == 1
    payload = notifications.first().payload
    assert payload["partial"] is True
    assert payload["catalogs_skipped"] == ["cat_b"]

    assert _counter(catalog="cat_b") == skips_before + 1

    # R18: the per-object record says cat_b was skipped, not searched.
    record = ObjectCrossmatchRecord.objects.get(alert=alert)
    assert record.catalog_outcomes == {
        "cat_a": "searched",
        "cat_b": "skipped_read_failure",
    }


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_all_catalogs_fail_reverts(monkeypatch):
    # R3 / AE3: when BOTH catalogs' reads fail transiently, the >=1-success guard
    # fails the batch closed -- it reverts to INGESTED with no matches or
    # notifications, rather than publishing an empty crossmatch. (Transient reads
    # surface as the fsspec-wrapped TypeError, which hits the skip arm; the guard
    # then trips because no catalog succeeded.)
    alert = AlertFactory(status=Alert.Status.QUEUED)

    def _boom(*a, **k):
        raise TypeError("can't concat ServerDisconnectedError to bytes")

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _boom)
    a_before = _counter(catalog="cat_a")
    b_before = _counter(catalog="cat_b")

    with pytest.raises(Exception):
        crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.INGESTED
    assert CatalogMatch.objects.filter(alert=alert).count() == 0
    assert Notification.objects.filter(alert=alert).count() == 0
    assert not ObjectCrossmatchRecord.objects.filter(alert=alert).exists()
    # Both catalogs skip (transient) before the guard reverts, so both counters
    # advance even though the batch itself reverted.
    assert _counter(catalog="cat_a") == a_before + 1
    assert _counter(catalog="cat_b") == b_before + 1


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_deterministic_error_fails_loud_not_skipped(monkeypatch):
    # Regression guard: a DETERMINISTIC error (a bad/missing-column ValueError
    # from _get_catalog) on cat_b must fail the batch loud even though cat_a read
    # fine -- it must NOT be swallowed as a skip, which would silently drop the
    # misconfigured catalog from every future batch. cat_a reads successfully
    # (empty) so the failure cannot be attributed to a zero-success batch.
    alert = AlertFactory(status=Alert.Status.QUEUED)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "cat_a":
            return pd.DataFrame()  # read fine, no matches
        raise ValueError(
            "cat_b: requested columns not found in catalog schema: ['mag']"
        )

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)
    b_before = _counter(catalog="cat_b")

    with pytest.raises(Exception):
        crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.INGESTED  # reverted, not silently MATCHED
    assert Notification.objects.filter(alert=alert).count() == 0
    # A deterministic error is fail-loud, not a skip: the skip counter must NOT
    # advance for cat_b (else operators would chase a phantom outage).
    assert _counter(catalog="cat_b") == b_before


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_partial_mark_stamps_every_notification(monkeypatch):
    # R4: the coverage stamp must land on EVERY published notification, not just
    # the first. cat_a matches two alerts (two notifications), cat_b skips.
    alert1 = AlertFactory(status=Alert.Status.QUEUED)
    alert2 = AlertFactory(status=Alert.Status.QUEUED)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "cat_a":
            return _match_rows(alert1, alert2)
        raise TypeError("can't concat ServerDisconnectedError to bytes")

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)

    crossmatch_batch([str(alert1.uuid), str(alert2.uuid)])

    notifications = Notification.objects.filter(alert__in=[alert1, alert2])
    assert notifications.count() == 2
    for notification in notifications:
        assert notification.payload["partial"] is True
        assert notification.payload["catalogs_skipped"] == ["cat_b"]


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_no_skip_leaves_payload_full(monkeypatch):
    # AE4 boundary: when every catalog reads successfully, no coverage mark is
    # applied -- the payload keeps the build-time default (partial=False,
    # catalogs_skipped=[]).
    alert = AlertFactory(status=Alert.Status.QUEUED)

    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )

    crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.MATCHED
    for notification in Notification.objects.filter(alert=alert):
        assert notification.payload["partial"] is False
        assert notification.payload["catalogs_skipped"] == []
