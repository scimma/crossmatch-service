"""Tests for the TNS snapshot-refresh task (plan U4)."""

import pytest
from django.utils import timezone

from core import tns as tns_client
from core.models import TnsObject, TnsSnapshotMeta
from tasks import tns as refresh_module
from tasks.schedule import RefreshTnsSnapshot, periodic_tasks


def _record(objid, ra=180.0, dec=-30.0, name=None, type=None, redshift=None):
    return tns_client.TnsObjectRecord(
        objid=objid,
        name=name or str(objid),
        name_prefix="SN",
        ra_deg=ra,
        dec_deg=dec,
        type=type,
        redshift=redshift,
    )


@pytest.fixture
def creds(settings):
    settings.TNS_BOT_ID = "123"
    settings.TNS_BOT_NAME = "testbot"
    settings.TNS_BOT_API_KEY = "secret"
    return settings


def _patch_fetch(monkeypatch, records, capture=None):
    def fake_fetch(*, base_url, filename, bot_id, bot_name, api_key, **kw):
        if capture is not None:
            capture["filename"] = filename
        return records

    monkeypatch.setattr(refresh_module.tns_client, "fetch_object_records", fake_fetch)


@pytest.mark.django_db
def test_refresh_skipped_without_credentials(settings):
    settings.TNS_BOT_ID = ""
    settings.TNS_BOT_API_KEY = ""
    result = refresh_module.refresh_snapshot()
    assert result["status"] == "skipped"
    assert TnsObject.objects.count() == 0
    assert TnsSnapshotMeta.objects.first() is None


@pytest.mark.django_db
def test_refresh_seeds_full_file_and_records_epoch(creds, monkeypatch):
    capture = {}
    _patch_fetch(monkeypatch, [_record(1), _record(2)], capture)

    result = refresh_module.refresh_snapshot()

    assert result == {"status": "ok", "seed": True, "upserted": 2}
    assert capture["filename"] == tns_client.FULL_OBJECTS_FILENAME
    assert TnsObject.objects.count() == 2
    # healpix_ipix computed for each row (cone pre-filter depends on it).
    assert all(o.healpix_ipix is not None for o in TnsObject.objects.all())
    meta = TnsSnapshotMeta.objects.get(pk=1)
    assert meta.last_refresh_epoch is not None


@pytest.mark.django_db
def test_refresh_merges_delta_when_snapshot_current(creds, monkeypatch):
    # Seed first.
    _patch_fetch(monkeypatch, [_record(1, name="old")])
    refresh_module.refresh_snapshot()

    # A current snapshot -> the next refresh uses the hourly delta and upserts by objid.
    capture = {}
    _patch_fetch(monkeypatch, [_record(1, name="new"), _record(3)], capture)
    result = refresh_module.refresh_snapshot()

    assert result["seed"] is False
    assert capture["filename"].startswith("tns_public_objects_")
    assert capture["filename"] != tns_client.FULL_OBJECTS_FILENAME
    assert TnsObject.objects.count() == 2  # objid 1 updated, objid 3 inserted
    assert TnsObject.objects.get(objid=1).name == "new"


@pytest.mark.django_db
def test_refresh_fail_soft_leaves_prior_snapshot(creds, monkeypatch):
    # Seed a snapshot.
    _patch_fetch(monkeypatch, [_record(1, name="kept")])
    refresh_module.refresh_snapshot()
    prior_epoch = TnsSnapshotMeta.objects.get(pk=1).last_refresh_epoch

    # Next refresh fails at download time.
    def boom(**kw):
        raise tns_client.TnsClientError("download exploded")

    monkeypatch.setattr(refresh_module.tns_client, "fetch_object_records", boom)
    result = refresh_module.refresh_snapshot()

    assert result["status"] == "failed"
    # Prior snapshot and epoch are untouched — no partial state.
    assert TnsObject.objects.count() == 1
    assert TnsObject.objects.get(objid=1).name == "kept"
    assert TnsSnapshotMeta.objects.get(pk=1).last_refresh_epoch == prior_epoch


def test_refresh_task_registered_with_interval(settings):
    handles = {t.task_handle for t in periodic_tasks}
    assert "refresh_tns_snapshot" in handles
    assert (
        RefreshTnsSnapshot.task_frequency_seconds
        == settings.TNS_SNAPSHOT_REFRESH_INTERVAL_SECONDS
    )
