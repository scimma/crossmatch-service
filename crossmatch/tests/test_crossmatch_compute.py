"""The shared crossmatch compute step (replay tool plan U1).

``compute_crossmatch`` runs the in-memory half of ``crossmatch_batch``: alert-frame
build, per-catalog crossmatch with skip/no-overlap classification, TNS enrichment,
and payload construction. Called without callbacks (as the replay tool does) it
must leave the database and the production metrics untouched (R6); production
persists through the ``on_tns`` / ``on_catalog`` callbacks so its write order is
unchanged (R7, KTD1).

The Dask/LSDB path is mocked at the same seams the ``crossmatch_batch`` tests use.
"""

import math
from datetime import timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.test import override_settings
from django.utils import timezone

import tasks.crossmatch as crossmatch_mod
from core.healpix import radec_to_ipix
from core.metrics import CATALOG_SKIPS, CROSSMATCH_MATCHES
from core.models import (
    Alert,
    CatalogMatch,
    Notification,
    TnsAssociation,
    TnsObject,
    TnsSnapshotMeta,
)
from tasks.crossmatch import (
    CATALOG_EMPTY,
    CATALOG_MATCHED,
    CATALOG_NO_OVERLAP,
    CATALOG_SKIPPED,
    compute_crossmatch,
    crossmatch_batch,
)
from tests.factories import AlertFactory

_BASE = {
    "hats_url": "x",
    "source_id_column": "source_id",
    "ra_column": "ra",
    "dec_column": "dec",
    "payload_columns": ["mag"],
}
ONE_CATALOG = [{**_BASE, "name": "cat_a"}]
TWO_CATALOGS = [{**_BASE, "name": "cat_a"}, {**_BASE, "name": "cat_b", "hats_url": "y"}]


@pytest.fixture(autouse=True)
def _mock_lsdb(monkeypatch):
    monkeypatch.setattr(
        crossmatch_mod.lsdb, "from_dataframe", lambda *a, **k: MagicMock()
    )


def _rows(*alerts):
    """Alert rows in the shape crossmatch_batch loads them (values_list tuples)."""
    return [(a.uuid, a.lsst_diaObject_diaObjectId, a.ra_deg, a.dec_deg) for a in alerts]


def _match_row(alert, source_id="cat-0", mag=18.2):
    return pd.DataFrame(
        [
            {
                "lsst_diaObject_diaObjectId": alert.lsst_diaObject_diaObjectId,
                "source_id": source_id,
                "_dist_arcsec": 0.4,
                "ra": 180.0,
                "dec": -30.0,
                "mag": mag,
            }
        ]
    )


def _seed_current_tns(ra=180.0, dec=-30.0):
    now = timezone.now()
    TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=now)
    TnsObject.objects.create(
        objid=1,
        name="2026abc",
        name_prefix="SN",
        ra_deg=ra,
        dec_deg=dec,
        type="SN Ia",
        redshift=0.05,
        healpix_ipix=radec_to_ipix(ra, dec),
    )


def _transient(*a, **k):
    raise TypeError("can't concat ServerDisconnectedError to bytes")


def _counter(metric, **labels):
    return metric.labels(**labels)._value.get()


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_compute_without_callbacks_has_no_side_effects(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    _seed_current_tns()
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )
    matches_before = _counter(CROSSMATCH_MATCHES, catalog="cat_a")

    result = compute_crossmatch(_rows(alert))

    assert len(result.records) == 1
    record = result.records[0]
    assert record.dia_object_id == alert.lsst_diaObject_diaObjectId
    assert record.catalog_name == "cat_a"
    assert record.source_id == "cat-0"
    assert record.catalog_payload == {"mag": 18.2}
    assert record.published_payload["catalog_payload"] == {"mag": 18.2}
    assert record.published_payload["tns_checked"] is True
    assert record.published_payload["tns"]["objid"] == 1
    assert result.catalog_outcomes == {"cat_a": CATALOG_MATCHED}
    assert result.tns.current is True

    assert CatalogMatch.objects.count() == 0
    assert Notification.objects.count() == 0
    assert TnsAssociation.objects.count() == 0
    alert.refresh_from_db()
    assert alert.status == Alert.Status.QUEUED
    assert alert.notified_at is None
    assert _counter(CROSSMATCH_MATCHES, catalog="cat_a") == matches_before


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_compute_payload_matches_what_production_publishes(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )

    computed = compute_crossmatch(_rows(alert)).records[0].published_payload
    crossmatch_batch([str(alert.uuid)])

    assert Notification.objects.get(alert=alert).payload == computed


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_compute_marks_skipped_catalog_and_stamps_partial(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    skips_before = _counter(CATALOG_SKIPS, catalog="cat_b")

    def _xmatch(alerts_catalog, cfg):
        if cfg["name"] == "cat_b":
            _transient()
        return _match_row(alert)

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _xmatch)

    result = compute_crossmatch(_rows(alert))

    assert result.catalog_outcomes == {
        "cat_a": CATALOG_MATCHED,
        "cat_b": CATALOG_SKIPPED,
    }
    payload = result.records[0].published_payload
    assert payload["partial"] is True
    assert payload["catalogs_skipped"] == ["cat_b"]
    # The skip counter belongs to production's callback, not to compute.
    assert _counter(CATALOG_SKIPS, catalog="cat_b") == skips_before


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_compute_records_no_overlap_as_its_own_outcome(monkeypatch):
    # Covers AE5.
    alert = AlertFactory(status=Alert.Status.QUEUED)

    def _xmatch(alerts_catalog, cfg):
        if cfg["name"] == "cat_a":
            raise RuntimeError("Catalogs do not overlap")
        return pd.DataFrame()

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _xmatch)

    result = compute_crossmatch(_rows(alert))

    assert result.catalog_outcomes == {
        "cat_a": CATALOG_NO_OVERLAP,
        "cat_b": CATALOG_EMPTY,
    }
    assert result.records == []


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_compute_raises_when_every_catalog_fails(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _transient)

    with pytest.raises(RuntimeError, match="catalogs failed to read"):
        compute_crossmatch(_rows(alert))


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_compute_with_no_valid_coordinates_touches_nothing(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    called = []
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: called.append(1)
    )

    result = compute_crossmatch(
        [(alert.uuid, alert.lsst_diaObject_diaObjectId, math.nan, math.nan)]
    )

    assert result.alert_count == 1
    assert result.crossmatched_count == 0
    assert result.records == []
    assert called == []
    alert.refresh_from_db()
    assert alert.status == Alert.Status.QUEUED
    assert alert.notified_at is None


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=[])
def test_compute_with_no_rows_returns_empty_result():
    result = compute_crossmatch([])

    assert result.alert_count == 0
    assert result.records == []


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_callbacks_run_tns_first_then_each_catalog_in_order(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )
    calls = []

    compute_crossmatch(
        _rows(alert),
        on_tns=lambda tns: calls.append("tns"),
        on_catalog=lambda name, outcome, records: calls.append(
            (name, outcome, len(records))
        ),
    )

    assert calls == [
        "tns",
        ("cat_a", CATALOG_MATCHED, 1),
        ("cat_b", CATALOG_MATCHED, 1),
    ]


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_soft_time_limit_mid_row_propagates(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )

    def _soft(*a, **k):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(crossmatch_mod, "build_published_payload", _soft)

    with pytest.raises(SoftTimeLimitExceeded):
        compute_crossmatch(_rows(alert))


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_stale_tns_snapshot_is_reported_not_current(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    TnsSnapshotMeta.objects.create(
        pk=1, last_refresh_epoch=timezone.now() - timedelta(days=2)
    )
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )

    result = compute_crossmatch(_rows(alert))

    assert result.tns.current is False
    assert result.tns.epoch is None
    assert result.records[0].published_payload["tns_checked"] is False
