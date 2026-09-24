"""Replay sample export and loading (replay tool plan U3).

The export selects historical alerts per coverage category (R2) inside a
read-only transaction (R1), writes a portable file, and the loader hands back
rows in the shape production loads them, with diaObjectId as an exact int (R3).
"""

import json
from uuid import UUID

import pytest
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import override_settings

from core.models import Alert
from replay.sample import (
    SAMPLE_FORMAT_VERSION,
    SampleFormatError,
    load_sample,
    read_only_transaction,
    select_sample,
    write_sample,
)
from tests.factories import AlertFactory, CatalogMatchFactory

_BASE = {
    "hats_url": "x",
    "source_id_column": "source_id",
    "ra_column": "ra",
    "dec_column": "dec",
    "payload_columns": ["mag"],
}
CATALOGS = [{**_BASE, "name": "cat_a"}, {**_BASE, "name": "cat_b"}]


def _terminal_alert(**kwargs):
    return AlertFactory(status=Alert.Status.NOTIFIED, **kwargs)


def _seed_every_category():
    a = _terminal_alert()
    CatalogMatchFactory(alert=a, catalog_name="cat_a", catalog_payload={"mag": 18.0})
    b = _terminal_alert()
    CatalogMatchFactory(alert=b, catalog_name="cat_b", catalog_payload={"mag": None})
    no_match = _terminal_alert()
    north = _terminal_alert(dec_deg=45.0)
    return a, b, no_match, north


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=CATALOGS)
def test_sample_covers_every_category():
    a, b, no_match, north = _seed_every_category()

    sample = select_sample(per_category=10, seed="s1")

    by_id = {row["dia_object_id"]: row for row in sample["alerts"]}
    assert "matched:cat_a" in by_id[a.lsst_diaObject_diaObjectId]["categories"]
    assert "matched:cat_b" in by_id[b.lsst_diaObject_diaObjectId]["categories"]
    assert "null_catalog_values" in by_id[b.lsst_diaObject_diaObjectId]["categories"]
    assert "no_match" in by_id[no_match.lsst_diaObject_diaObjectId]["categories"]
    assert "outside_des" in by_id[north.lsst_diaObject_diaObjectId]["categories"]
    assert sample["unfilled_categories"] == []
    assert sample["format_version"] == SAMPLE_FORMAT_VERSION


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=CATALOGS)
def test_unfilled_category_is_reported():
    a = _terminal_alert()
    CatalogMatchFactory(alert=a, catalog_name="cat_a", catalog_payload={"mag": 18.0})

    sample = select_sample(per_category=10, seed="s1")

    assert "null_catalog_values" in sample["unfilled_categories"]
    assert "matched:cat_b" in sample["unfilled_categories"]
    assert sample["categories"]["matched:cat_a"] == 1


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=CATALOGS)
def test_same_seed_reproduces_the_sample_and_cap_applies():
    for _ in range(8):
        _terminal_alert()

    first = select_sample(per_category=3, seed="s1")
    again = select_sample(per_category=3, seed="s1")

    assert first["alerts"] == again["alerts"]
    assert first["categories"]["no_match"] == 3
    assert len(first["alerts"]) == 3


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=CATALOGS)
def test_alert_in_two_categories_appears_once():
    a = _terminal_alert(dec_deg=45.0)
    CatalogMatchFactory(alert=a, catalog_name="cat_a", catalog_payload={"mag": 1.0})

    sample = select_sample(per_category=10, seed="s1")

    rows = [
        r
        for r in sample["alerts"]
        if r["dia_object_id"] == a.lsst_diaObject_diaObjectId
    ]
    assert len(rows) == 1
    assert set(rows[0]["categories"]) == {"matched:cat_a", "outside_des"}


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=CATALOGS)
def test_non_terminal_alerts_are_not_no_match():
    AlertFactory(status=Alert.Status.INGESTED)

    sample = select_sample(per_category=10, seed="s1")

    assert sample["alerts"] == []


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=CATALOGS)
def test_large_dia_object_id_round_trips_exactly(tmp_path):
    big = 2**62 + 12345
    alert = _terminal_alert(lsst_diaObject_diaObjectId=big)

    path = tmp_path / "sample.json"
    write_sample(select_sample(per_category=10, seed="s1"), path)
    loaded = load_sample(path)

    assert loaded.rows == [(UUID(str(alert.uuid)), big, 180.0, -30.0)]
    assert isinstance(loaded.rows[0][1], int)
    assert loaded.digest


def _write(tmp_path, data):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(data))
    return path


def test_loader_rejects_unknown_format_version(tmp_path):
    path = _write(
        tmp_path,
        {"kind": "crossmatch-replay-sample", "format_version": 99, "alerts": []},
    )
    with pytest.raises(SampleFormatError, match="format_version"):
        load_sample(path)


def test_loader_rejects_float_dia_object_id(tmp_path):
    path = _write(
        tmp_path,
        {
            "kind": "crossmatch-replay-sample",
            "format_version": SAMPLE_FORMAT_VERSION,
            "alerts": [
                {
                    "uuid": "00000000-0000-0000-0000-000000000001",
                    "dia_object_id": 9.0e9,
                    "ra_deg": 1.0,
                    "dec_deg": 2.0,
                    "categories": ["no_match"],
                }
            ],
        },
    )
    with pytest.raises(SampleFormatError, match="dia_object_id"):
        load_sample(path)


@pytest.mark.django_db(transaction=True)
def test_read_only_transaction_rejects_writes():
    with pytest.raises(DatabaseError):
        with read_only_transaction():
            AlertFactory()
    assert Alert.objects.count() == 0


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=CATALOGS)
def test_export_command_writes_sample_file(tmp_path):
    _seed_every_category()
    path = tmp_path / "out.json"

    call_command(
        "replay_export_sample",
        "--output",
        str(path),
        "--per-category",
        "5",
        "--seed",
        "abc",
    )

    data = json.loads(path.read_text())
    assert data["kind"] == "crossmatch-replay-sample"
    assert data["seed"] == "abc"
    assert data["per_category"] == 5
    assert len(data["alerts"]) == 4
