"""Replay run command and snapshot (replay tool plan U4).

``replay_run`` refuses unless replay is enabled (R8), the Dask cluster is
configured and aligned (R4a), and the image tag is known (R9); then it replays
the sample through the shared compute step with no side effects (R6, AE4) and
writes a snapshot with its run context (R9).
"""

import json
from unittest import mock
from unittest.mock import MagicMock

import pandas as pd
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

import replay.snapshot as snapshot_mod
import tasks.crossmatch as crossmatch_mod
from core.healpix import radec_to_ipix
from core.metrics import CROSSMATCH_MATCHES
from core.models import (
    Alert,
    CatalogMatch,
    Notification,
    TnsAssociation,
    TnsObject,
    TnsSnapshotMeta,
)
from replay.sample import select_sample, write_sample
from replay.snapshot import SNAPSHOT_KIND, tns_fingerprint
from tests.factories import AlertFactory, CatalogMatchFactory

# The autouse fixture stubs catalog_identity for the command tests; keep the real one.
_real_catalog_identity = snapshot_mod.catalog_identity

CATALOGS = [
    {
        "name": "cat_a",
        "hats_url": "https://example.test/cat_a",
        "source_id_column": "source_id",
        "ra_column": "ra",
        "dec_column": "dec",
        "payload_columns": ["mag"],
    }
]
REPLAY_SETTINGS = dict(
    CROSSMATCH_CATALOGS=CATALOGS,
    CROSSMATCH_REPLAY_ENABLED=True,
    DASK_SCHEDULER_ADDRESS="tcp://sched:8786",
    APP_VERSION="0.12.0",
)
VERSIONS = {
    "client": {"lsdb": "0.10.4", "nested_pandas": "0.6.10"},
    "scheduler": {"dask": "2026.1.2"},
    "workers": {"tcp://w1": {"lsdb": "0.10.4", "nested_pandas": "0.6.10"}},
}


@pytest.fixture(autouse=True)
def _mock_dask_and_lsdb(monkeypatch):
    monkeypatch.setattr(
        crossmatch_mod.lsdb, "from_dataframe", lambda *a, **k: MagicMock()
    )
    client = MagicMock()
    monkeypatch.setattr(
        snapshot_mod, "check_cluster_alignment", lambda address, timeout: (client, [])
    )
    monkeypatch.setattr(snapshot_mod, "cluster_package_versions", lambda c: VERSIONS)
    monkeypatch.setattr(
        snapshot_mod,
        "catalog_identity",
        lambda cfg: {"hats_url": cfg["hats_url"], "total_rows": 100},
    )
    return client


def _sample_file(tmp_path, alert):
    CatalogMatchFactory(alert=alert, catalog_name="cat_a", catalog_payload={"mag": 1.0})
    path = tmp_path / "sample.json"
    write_sample(select_sample(per_category=10, seed="s"), path)
    return path


def _match_row(alert):
    return pd.DataFrame(
        [
            {
                "lsst_diaObject_diaObjectId": alert.lsst_diaObject_diaObjectId,
                "source_id": "src-1",
                "_dist_arcsec": 0.4,
                "ra": 180.0,
                "dec": -30.0,
                "mag": 18.2,
            }
        ]
    )


def _run(tmp_path, sample, *extra):
    out = tmp_path / "snap.json"
    call_command("replay_run", "--sample", str(sample), "--output", str(out), *extra)
    return json.loads(out.read_text())


@pytest.mark.django_db
@override_settings(**{**REPLAY_SETTINGS, "CROSSMATCH_REPLAY_ENABLED": False})
def test_refuses_when_replay_disabled(tmp_path, monkeypatch):
    alert = AlertFactory(status=Alert.Status.NOTIFIED)
    sample = _sample_file(tmp_path, alert)
    monkeypatch.setattr(
        snapshot_mod, "check_cluster_alignment", mock.Mock(side_effect=AssertionError)
    )
    with pytest.raises(CommandError, match="CROSSMATCH_REPLAY_ENABLED"):
        _run(tmp_path, sample)


@pytest.mark.django_db
@override_settings(**{**REPLAY_SETTINGS, "DASK_SCHEDULER_ADDRESS": ""})
def test_refuses_without_scheduler_address(tmp_path):
    sample = _sample_file(tmp_path, AlertFactory(status=Alert.Status.NOTIFIED))
    with pytest.raises(CommandError, match="DASK_SCHEDULER_ADDRESS"):
        _run(tmp_path, sample)


@pytest.mark.django_db
@override_settings(**{**REPLAY_SETTINGS, "APP_VERSION": "0.0.0"})
def test_refuses_default_image_tag_unless_given(tmp_path, monkeypatch):
    alert = AlertFactory(status=Alert.Status.NOTIFIED)
    sample = _sample_file(tmp_path, alert)
    with pytest.raises(CommandError, match="image tag"):
        _run(tmp_path, sample)

    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )
    snap = _run(tmp_path, sample, "--image-tag", "0.12.1")
    assert snap["run_context"]["image_tag"] == "0.12.1"


@pytest.mark.django_db
@override_settings(**REPLAY_SETTINGS)
def test_refuses_on_version_drift(tmp_path, monkeypatch, _mock_dask_and_lsdb):
    sample = _sample_file(tmp_path, AlertFactory(status=Alert.Status.NOTIFIED))
    drift = [
        {
            "package": "lsdb",
            "client_version": "0.11.0",
            "scheduler_version": None,
            "worker_versions": {"tcp://w1": "0.10.4"},
        }
    ]
    monkeypatch.setattr(
        snapshot_mod,
        "check_cluster_alignment",
        lambda address, timeout: (_mock_dask_and_lsdb, drift),
    )
    with pytest.raises(CommandError, match="lsdb"):
        _run(tmp_path, sample)
    _mock_dask_and_lsdb.close.assert_called()


@pytest.mark.django_db
@override_settings(**REPLAY_SETTINGS)
def test_snapshot_records_matches_and_run_context(tmp_path, monkeypatch):
    alert = AlertFactory(status=Alert.Status.NOTIFIED)
    sample = _sample_file(tmp_path, alert)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )

    snap = _run(tmp_path, sample)

    assert snap["kind"] == SNAPSHOT_KIND
    [match] = snap["matches"]
    assert match["dia_object_id"] == alert.lsst_diaObject_diaObjectId
    assert match["catalog"] == "cat_a"
    assert match["source_id"] == "src-1"
    assert match["published_payload"]["catalog_payload"] == {"mag": 18.2}
    ctx = snap["run_context"]
    assert ctx["image_tag"] == "0.12.0"
    assert ctx["versions"] == VERSIONS
    assert ctx["catalog_outcomes"] == {"cat_a": "matched"}
    assert ctx["crossmatch"]["radius_arcsec"] > 0
    assert ctx["crossmatch"]["catalogs"][0]["hats_url"] == "https://example.test/cat_a"
    assert ctx["catalog_identity"]["cat_a"]["total_rows"] == 100
    assert ctx["sample"]["digest"]
    assert ctx["sample"]["alert_count"] == 1
    assert ctx["tns"]["current"] is False


@pytest.mark.django_db
@override_settings(**REPLAY_SETTINGS)
def test_replay_has_no_side_effects(tmp_path, monkeypatch):
    # Covers AE4.
    alert = AlertFactory(status=Alert.Status.NOTIFIED)
    sample = _sample_file(tmp_path, alert)
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )
    matches_before = CatalogMatch.objects.count()
    counter_before = CROSSMATCH_MATCHES.labels(catalog="cat_a")._value.get()

    _run(tmp_path, sample)

    assert CatalogMatch.objects.count() == matches_before
    assert Notification.objects.count() == 0
    assert TnsAssociation.objects.count() == 0
    alert.refresh_from_db()
    assert alert.status == Alert.Status.NOTIFIED
    assert CROSSMATCH_MATCHES.labels(catalog="cat_a")._value.get() == counter_before


@pytest.mark.django_db
@override_settings(**REPLAY_SETTINGS)
def test_current_tns_is_recorded_with_fingerprints(tmp_path, monkeypatch):
    alert = AlertFactory(status=Alert.Status.NOTIFIED)
    sample = _sample_file(tmp_path, alert)
    TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())
    TnsObject.objects.create(
        objid=7,
        name="2026x",
        name_prefix="SN",
        ra_deg=180.0,
        dec_deg=-30.0,
        type="SN II",
        redshift=0.02,
        healpix_ipix=radec_to_ipix(180.0, -30.0),
    )
    monkeypatch.setattr(
        crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert)
    )

    snap = _run(tmp_path, sample)

    tns = snap["run_context"]["tns"]
    assert tns["current"] is True
    assert tns["epoch"]
    assert tns["content_identity"]
    assert str(alert.lsst_diaObject_diaObjectId) in tns["fingerprints"]
    assert snap["matches"][0]["published_payload"]["tns"]["objid"] == 7


@pytest.mark.django_db
def test_tns_fingerprint_changes_only_with_cone_content():
    TnsObject.objects.create(
        objid=7,
        name="2026x",
        name_prefix="SN",
        ra_deg=180.0,
        dec_deg=-30.0,
        type="SN II",
        redshift=0.02,
        healpix_ipix=radec_to_ipix(180.0, -30.0),
    )
    first = tns_fingerprint(180.0, -30.0, 5.0)
    assert tns_fingerprint(180.0, -30.0, 5.0) == first

    # A far-away object does not change this alert's fingerprint.
    TnsObject.objects.create(
        objid=8,
        name="2026y",
        name_prefix="SN",
        ra_deg=10.0,
        dec_deg=10.0,
        type="SN Ia",
        redshift=0.1,
        healpix_ipix=radec_to_ipix(10.0, 10.0),
    )
    assert tns_fingerprint(180.0, -30.0, 5.0) == first

    TnsObject.objects.filter(objid=7).update(redshift=0.03)
    assert tns_fingerprint(180.0, -30.0, 5.0) != first


def test_catalog_identity_records_build_properties(monkeypatch):
    import matching.catalog as catalog_mod

    fake = MagicMock()
    fake.hc_structure.catalog_info.total_rows = 42
    fake.hc_structure.catalog_info.model_extra = {"hats_creation_date": "2026-01-02"}
    monkeypatch.setattr(catalog_mod, "_get_catalog", lambda cfg: fake)

    identity = _real_catalog_identity(CATALOGS[0])

    assert identity == {
        "hats_url": CATALOGS[0]["hats_url"],
        "total_rows": 42,
        "hats_creation_date": "2026-01-02",
    }


def test_catalog_identity_records_error_instead_of_raising(monkeypatch):
    import matching.catalog as catalog_mod

    def _boom(cfg):
        raise OSError("unreachable")

    monkeypatch.setattr(catalog_mod, "_get_catalog", _boom)

    identity = _real_catalog_identity(CATALOGS[0])

    assert identity == {"hats_url": CATALOGS[0]["hats_url"], "error": "unreachable"}
