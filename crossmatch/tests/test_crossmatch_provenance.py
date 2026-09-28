"""Per-object crossmatch provenance and per-catalog search outcomes (plan U2).

Every object that becomes MATCHED from this release on gets one
``ObjectCrossmatchRecord`` per ``(alert, match_version)``: every catalog's
search outcome (KTD6), the brokers that had delivered it, and a reference to a
content-hashed ``ProvenanceSet`` (radius, catalog releases, broker cuts; KTD5).
The record write can never revert a batch.

The Dask/LSDB path is mocked at the same two seams the ``crossmatch_batch``
tests use (``lsdb.from_dataframe`` and ``crossmatch_alerts``), plus the
coverage-map seam ``catalog_moc`` so each test controls the footprint.
"""

import json
from unittest.mock import MagicMock

import pandas as pd
import pytest
from django.db import connection
from django.test import override_settings
from mocpy import MOC

import matching.catalog as catalog_mod
import tasks.crossmatch as crossmatch_mod
from core.metrics import CROSSMATCH_RECORD_FAILURES
from core.models import (
    Alert,
    AlertDelivery,
    Notification,
    ObjectCrossmatchRecord,
    ProvenanceSet,
)
from core.provenance import catalog_releases, reliability_cuts
from matching.catalog import CatalogCoverageUnavailable
from tasks.crossmatch import compute_crossmatch, crossmatch_batch
from tests.factories import AlertDeliveryFactory, AlertFactory, CatalogMatchFactory

_BASE = {
    "source_id_column": "source_id",
    "ra_column": "ra",
    "dec_column": "dec",
    "payload_columns": ["mag"],
}
GAIA = {**_BASE, "name": "gaia", "hats_url": "x", "release": "Gaia DR3"}
DES = {**_BASE, "name": "des", "hats_url": "y", "release": "DES Y6 Gold"}
TWO_CATALOGS = [GAIA, DES]

# Order-0 pixel 0 covers RA 0-90 deg in the northern cap region, so (45, 30) is
# inside and the factory default (180, -30) is outside.
NORTH_EAST = MOC.from_string("0/0")
FULL_SKY = MOC.from_string("0/0-11")

TRANSIENT = TypeError("can't concat ServerDisconnectedError to bytes")


def _match_rows(*alerts):
    return pd.DataFrame(
        [
            {
                "lsst_diaObject_diaObjectId": alert.lsst_diaObject_diaObjectId,
                "source_id": f"src-{i}",
                "_dist_arcsec": 0.4,
                "ra": alert.ra_deg,
                "dec": alert.dec_deg,
                "mag": 18.2,
            }
            for i, alert in enumerate(alerts)
        ]
    )


@pytest.fixture(autouse=True)
def _mock_lsdb(monkeypatch):
    monkeypatch.setattr(
        crossmatch_mod.lsdb, "from_dataframe", lambda *a, **k: MagicMock()
    )


def _mocs(monkeypatch, mocs):
    monkeypatch.setattr(crossmatch_mod, "catalog_moc", lambda cfg: mocs[cfg["name"]])


def _outcomes(alert):
    return ObjectCrossmatchRecord.objects.get(alert=alert).catalog_outcomes


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_ae8_skipped_read_and_outside_footprint(monkeypatch):
    # AE8: Gaia's read fails after retries; DES reads fine but one object falls
    # outside DES's coverage map. That object records Gaia skipped and DES
    # outside the footprint -- neither is "searched".
    inside = AlertFactory(status=Alert.Status.QUEUED, ra_deg=45.0, dec_deg=30.0)
    outside = AlertFactory(status=Alert.Status.QUEUED)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "gaia":
            raise TRANSIENT
        return _match_rows(inside)

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)
    _mocs(monkeypatch, {"gaia": FULL_SKY, "des": NORTH_EAST})

    crossmatch_batch([str(inside.uuid), str(outside.uuid)])

    assert _outcomes(outside) == {
        "gaia": "skipped_read_failure",
        "des": "outside_footprint",
    }
    assert _outcomes(inside) == {"gaia": "skipped_read_failure", "des": "searched"}


def _failures(reason):
    return CROSSMATCH_RECORD_FAILURES.labels(reason=reason)._value.get()


def _no_overlap(*a, **k):
    raise RuntimeError("Catalogs do not overlap")


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_whole_batch_no_overlap_records_outside_footprint(monkeypatch):
    alerts = [AlertFactory(status=Alert.Status.QUEUED) for _ in range(2)]

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "des":
            _no_overlap()
        return pd.DataFrame()

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)

    crossmatch_batch([str(a.uuid) for a in alerts])

    for alert in alerts:
        assert _outcomes(alert) == {"gaia": "searched", "des": "outside_footprint"}


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_mixed_batch_records_invalid_positions(monkeypatch):
    valid = AlertFactory(status=Alert.Status.QUEUED)
    invalid = AlertFactory(status=Alert.Status.QUEUED, ra_deg=float("nan"))

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "gaia":
            raise TRANSIENT
        return pd.DataFrame()

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)

    crossmatch_batch([str(valid.uuid), str(invalid.uuid)])

    assert _outcomes(valid) == {"gaia": "skipped_read_failure", "des": "searched"}
    assert _outcomes(invalid) == {
        "gaia": "not_searched_invalid_position",
        "des": "not_searched_invalid_position",
    }


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_all_invalid_batch_records_on_early_return(monkeypatch):
    # The all-invalid batch never reaches compute_crossmatch or the final
    # atomic block; it must still record every catalog as not searched.
    alerts = [
        AlertFactory(status=Alert.Status.QUEUED, dec_deg=float("nan"))
        for _ in range(2)
    ]
    reads = MagicMock()
    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", reads)

    crossmatch_batch([str(a.uuid) for a in alerts])

    reads.assert_not_called()
    for alert in alerts:
        alert.refresh_from_db()
        assert alert.status == Alert.Status.MATCHED
        assert _outcomes(alert) == {
            "gaia": "not_searched_invalid_position",
            "des": "not_searched_invalid_position",
        }


@pytest.mark.django_db
@override_settings(
    CROSSMATCH_CATALOGS=TWO_CATALOGS,
    ANTARES_DECLARED_MIN_RELIABILITY=0.6,
    ANTARES_DECLARED_MIN_RELIABILITY_AS_OF="2026-09-01",
    MIN_DIASOURCE_RELIABILITY=0.5,
)
def test_records_delivering_brokers_frozen_at_matched(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    AlertDeliveryFactory(alert=alert, broker="pittgoogle")
    AlertDeliveryFactory(alert=alert, broker="antares")
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: pd.DataFrame()
    )

    crossmatch_batch([str(alert.uuid)])
    # A delivery after MATCHED does not change the record.
    AlertDeliveryFactory(alert=alert, broker="lasair")

    record = ObjectCrossmatchRecord.objects.get(alert=alert)
    assert record.brokers == ["antares", "pittgoogle"]
    cuts = {c["broker"]: c for c in record.provenance_set.reliability_cuts}
    assert cuts["antares"] == {
        "broker": "antares",
        "min_reliability": 0.6,
        "enforced_by": "broker",
        "status": "declared",
        "as_of": "2026-09-01",
    }
    assert cuts["pittgoogle"]["min_reliability"] == 0.5
    assert cuts["pittgoogle"]["enforced_by"] == "service"
    assert set(AlertDelivery.objects.filter(alert=alert).values_list(
        "broker", flat=True)) == {"antares", "lasair", "pittgoogle"}


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_rerun_with_gaia_skipped_keeps_gaia_searched(monkeypatch):
    # A first run persisted Gaia matches, then reverted (e.g. a later failure).
    # The rerun skips Gaia; the stored matches still say Gaia was searched.
    alert = AlertFactory(status=Alert.Status.QUEUED)
    CatalogMatchFactory(alert=alert, catalog_name="gaia", match_version=1)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "gaia":
            raise TRANSIENT
        return pd.DataFrame()

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)

    crossmatch_batch([str(alert.uuid)])

    assert _outcomes(alert) == {"gaia": "searched", "des": "searched"}


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_rerun_matches_of_other_version_do_not_force_searched(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    CatalogMatchFactory(alert=alert, catalog_name="gaia", match_version=2)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "gaia":
            raise TRANSIENT
        return pd.DataFrame()

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)

    crossmatch_batch([str(alert.uuid)], match_version=1)

    assert _outcomes(alert)["gaia"] == "skipped_read_failure"


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_two_runs_leave_one_record_with_later_outcomes(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: pd.DataFrame()
    )
    crossmatch_batch([str(alert.uuid)])
    first = ObjectCrossmatchRecord.objects.get(alert=alert)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "des":
            _no_overlap()
        return pd.DataFrame()

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)
    crossmatch_batch([str(alert.uuid)])

    records = ObjectCrossmatchRecord.objects.filter(alert=alert, match_version=1)
    assert records.count() == 1
    second = records.get()
    assert second.catalog_outcomes == {"gaia": "searched", "des": "outside_footprint"}
    assert second.crossmatched_at >= first.crossmatched_at
    # Same settings: both runs share one provenance set.
    assert ProvenanceSet.objects.count() == 1


def _break_record_write(monkeypatch, sql):
    # A real database error from inside the record upsert, so the test proves
    # the savepoint keeps the enclosing MATCHED transaction usable.
    def _boom(*a, **k):
        with connection.cursor() as cursor:
            cursor.execute(sql)

    monkeypatch.setattr(ObjectCrossmatchRecord.objects, "bulk_create", _boom)


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_record_write_db_error_still_commits_matched(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod,
        "crossmatch_alerts",
        lambda *a, **k: _match_rows(alert),
    )
    _break_record_write(monkeypatch, "SELECT 1/0")
    before = _failures("write_failed")

    crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.MATCHED
    assert Notification.objects.filter(alert=alert).count() == 2
    assert not ObjectCrossmatchRecord.objects.filter(alert=alert).exists()
    assert _failures("write_failed") == before + 1


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_missing_record_table_is_logged_and_skipped(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: pd.DataFrame()
    )
    _break_record_write(monkeypatch, "SELECT * FROM no_such_record_table")
    before = _failures("missing_table")

    crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.MATCHED
    assert _failures("missing_table") == before + 1


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_catalog_without_coverage_map_records_nothing(monkeypatch):
    # KTD6 stop condition: no silent fallback to "searched". The batch itself
    # is unaffected; its objects simply get no record.
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_rows(alert)
    )

    def _no_moc(cfg):
        raise CatalogCoverageUnavailable(f"{cfg['name']}: no moc")

    monkeypatch.setattr(crossmatch_mod, "catalog_moc", _no_moc)

    crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.MATCHED
    assert Notification.objects.filter(alert=alert).count() == 2
    assert not ObjectCrossmatchRecord.objects.exists()


def test_catalog_moc_raises_when_catalog_has_no_coverage_map(monkeypatch):
    fake = MagicMock()
    fake.hc_structure.moc = None
    monkeypatch.setattr(catalog_mod, "_get_catalog", lambda cfg: fake)

    with pytest.raises(CatalogCoverageUnavailable, match="gaia"):
        catalog_mod.catalog_moc(GAIA)

    fake.hc_structure.moc = NORTH_EAST
    assert catalog_mod.catalog_moc(GAIA) is NORTH_EAST


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=[{**GAIA, "release": ""}])
def test_catalog_config_without_release_records_nothing(monkeypatch):
    # A catalog entry with no release label (possible only via a runtime
    # override) makes the provenance builder raise; the batch still commits.
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: pd.DataFrame()
    )
    before = _failures("build_failed")

    crossmatch_batch([str(alert.uuid)])

    alert.refresh_from_db()
    assert alert.status == Alert.Status.MATCHED
    assert not ObjectCrossmatchRecord.objects.exists()
    assert _failures("build_failed") == before + 1


@pytest.mark.django_db
@override_settings(
    CROSSMATCH_CATALOGS=TWO_CATALOGS,
    LASAIR_DECLARED_MIN_RELIABILITY=0.7,
    LASAIR_DECLARED_MIN_RELIABILITY_AS_OF="2026-08-15",
)
def test_record_round_trips_as_json_native(monkeypatch):
    alert = AlertFactory(status=Alert.Status.QUEUED)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_rows(alert)
    )

    crossmatch_batch([str(alert.uuid)])

    record = ObjectCrossmatchRecord.objects.select_related("provenance_set").get(
        alert=alert
    )
    pset = record.provenance_set
    content = {
        "crossmatch_radius_arcsec": pset.crossmatch_radius_arcsec,
        "catalogs": pset.catalogs,
        "reliability_cuts": pset.reliability_cuts,
    }
    assert json.loads(json.dumps(content, allow_nan=False)) == content
    assert isinstance(pset.crossmatch_radius_arcsec, float)
    assert pset.catalogs == catalog_releases()
    assert pset.reliability_cuts == reliability_cuts()
    lasair = next(c for c in pset.reliability_cuts if c["broker"] == "lasair")
    assert lasair["as_of"] == "2026-08-15"
    assert isinstance(lasair["min_reliability"], float)
    assert json.loads(json.dumps(record.catalog_outcomes)) == record.catalog_outcomes
    assert record.brokers == []
    assert record.crossmatched_at is not None
    assert len(pset.content_hash) == 64


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_published_payload_unchanged_by_recording(monkeypatch):
    # The same batch published with and without the record path must produce
    # byte-identical Hopskotch payloads (Goal Capsule stop condition).
    alert = AlertFactory(status=Alert.Status.QUEUED)

    def _dispatch(alerts_catalog, catalog_config):
        if catalog_config["name"] == "gaia":
            raise TRANSIENT
        return _match_rows(alert)

    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", _dispatch)
    rows = [(alert.uuid, alert.lsst_diaObject_diaObjectId, alert.ra_deg, alert.dec_deg)]

    def _no_moc(cfg):
        raise CatalogCoverageUnavailable("disabled")

    with monkeypatch.context() as m:
        m.setattr(crossmatch_mod, "catalog_moc", _no_moc)
        without = compute_crossmatch(rows)
    assert without.search_outcomes is None

    crossmatch_batch([str(alert.uuid)])

    published = [
        json.dumps(n.payload, sort_keys=True).encode()
        for n in Notification.objects.filter(alert=alert)
    ]
    expected = [
        json.dumps(r.published_payload, sort_keys=True, default=str).encode()
        for r in without.records
    ]
    assert published == expected
    assert ObjectCrossmatchRecord.objects.filter(alert=alert).exists()


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_compute_result_carries_search_outcomes(monkeypatch):
    inside = AlertFactory(ra_deg=45.0, dec_deg=30.0)
    outside = AlertFactory()
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: pd.DataFrame()
    )
    _mocs(monkeypatch, {"gaia": NORTH_EAST, "des": FULL_SKY})
    rows = [
        (a.uuid, a.lsst_diaObject_diaObjectId, a.ra_deg, a.dec_deg)
        for a in (inside, outside)
    ]

    result = compute_crossmatch(rows)

    assert result.search_outcomes == {
        "gaia": {
            inside.lsst_diaObject_diaObjectId: "searched",
            outside.lsst_diaObject_diaObjectId: "outside_footprint",
        },
        "des": {
            inside.lsst_diaObject_diaObjectId: "searched",
            outside.lsst_diaObject_diaObjectId: "searched",
        },
    }
    # compute_crossmatch writes nothing (the replay tool relies on this).
    assert not ObjectCrossmatchRecord.objects.exists()
