"""Snapshot comparison and report (replay tool plan U5).

Every output difference is listed (R10) and grouped by kind, catalog, and field
(R11); run-context differences and comparability breaks come first (R12); an
unchanged stack compares clean (R13). No database is needed.
"""

import copy
import json

import pytest
from django.core.management import call_command

from replay.compare import (
    MATCH_ONLY_IN_BASELINE,
    MATCH_ONLY_IN_CANDIDATE,
    SOURCE_CHANGED,
    TNS_CHANGED,
    TNS_DRIFT,
    TYPE_CHANGED,
    VALUE_CHANGED,
    compare_snapshots,
    render_report,
)
from replay.snapshot import SNAPSHOT_FORMAT_VERSION, SNAPSHOT_KIND


def _payload(dia_id, mag=18.2, tns=None, epoch="2026-09-24T10:00:00+00:00"):
    return {
        "lsst_diaObject_diaObjectId": dia_id,
        "ra": 180.0,
        "dec": -30.0,
        "catalog": "cat_a",
        "catalog_source_id": "src-1",
        "separation_arcsec": 0.4,
        "catalog_payload": {"mag": mag},
        "catalogs_skipped": [],
        "partial": False,
        "tns": tns,
        "tns_checked": True,
        "tns_snapshot_epoch": epoch,
    }


def _match(dia_id, catalog="cat_a", source_id="src-1", **payload_kwargs):
    payload = _payload(dia_id, **payload_kwargs)
    return {
        "dia_object_id": dia_id,
        "catalog": catalog,
        "source_id": source_id,
        "dist_arcsec": 0.4,
        "source_ra_deg": 180.0,
        "source_dec_deg": -30.0,
        "catalog_payload": payload["catalog_payload"],
        "published_payload": payload,
    }


def _snapshot(matches, fingerprint="fp-1", epoch="2026-09-24T10:00:00+00:00"):
    return {
        "kind": SNAPSHOT_KIND,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "run_context": {
            "started_at": "2026-09-24T10:00:00+00:00",
            "image_tag": "0.12.0",
            "versions": {
                "client": {"lsdb": "0.10.4", "pandas": "2.3.3"},
                "scheduler": {"dask": "2026.1.2"},
                "workers": {"tcp://10.0.0.1:1": {"lsdb": "0.10.4"}},
            },
            "sample": {"digest": "abc", "alert_count": 2},
            "crossmatch": {
                "radius_arcsec": 1.0,
                "catalogs": [
                    {
                        "name": "cat_a",
                        "hats_url": "u",
                        "source_id_column": "source_id",
                        "ra_column": "ra",
                        "dec_column": "dec",
                        "payload_columns": ["mag"],
                    }
                ],
                "tns_match_radius_arcsec": 1.0,
                "tns_snapshot_max_age_seconds": 7200,
                "tns_object_url_template": "https://tns/{name}",
            },
            "catalog_outcomes": {"cat_a": "matched"},
            "catalog_identity": {"cat_a": {"hats_url": "u", "total_rows": 10}},
            "tns": {
                "current": True,
                "epoch": epoch,
                "content_identity": "ci-" + fingerprint,
                "fingerprints": {"1": fingerprint, "2": "fp-2"},
            },
        },
        "matches": matches,
    }


def test_identical_snapshots_have_no_differences():
    # Covers AE1.
    snap = _snapshot([_match(1), _match(2)])
    result = compare_snapshots(snap, copy.deepcopy(snap))

    assert result.flags == []
    assert result.groups == []
    assert result.difference_count == 0
    assert "No output differences" in render_report(result, "a.json", "b.json")


def test_epoch_only_change_is_context_not_output():
    # Covers AE1: the epoch advanced, TNS content did not.
    base = _snapshot([_match(1)])
    cand = _snapshot(
        [_match(1, epoch="2026-09-24T11:00:00+00:00")],
        epoch="2026-09-24T11:00:00+00:00",
    )
    result = compare_snapshots(base, cand)

    assert result.difference_count == 0
    assert any(d.field == "tns.epoch" for d in result.context_differences)


def test_tns_difference_is_drift_only_when_fingerprint_changed():
    # Covers AE2.
    tns_a = {"objid": 1, "redshift": 0.05}
    tns_b = {"objid": 1, "redshift": 0.06}
    base = _snapshot([_match(1, tns=tns_a)])

    drifted = compare_snapshots(
        base, _snapshot([_match(1, tns=tns_b)], fingerprint="fp-9")
    )
    assert [g.kind for g in drifted.groups] == [TNS_DRIFT]

    same_content = compare_snapshots(base, _snapshot([_match(1, tns=tns_b)]))
    assert [g.kind for g in same_content.groups] == [TNS_CHANGED]


def test_radius_change_breaks_comparability_and_is_reported_first():
    # Covers AE3.
    base = _snapshot([_match(1)])
    cand = copy.deepcopy(base)
    cand["run_context"]["crossmatch"]["radius_arcsec"] = 2.0

    result = compare_snapshots(base, cand)
    report = render_report(result, "a.json", "b.json")

    assert any("radius" in flag for flag in result.flags)
    assert report.index("## Comparability") < report.index("## Output differences")


def test_skipped_catalog_breaks_comparability():
    base = _snapshot([_match(1)])
    cand = copy.deepcopy(base)
    cand["run_context"]["catalog_outcomes"]["cat_a"] = "skipped"

    result = compare_snapshots(base, cand)

    assert any("cat_a" in flag and "skipped" in flag for flag in result.flags)


def test_stale_tns_breaks_comparability():
    base = _snapshot([_match(1)])
    cand = copy.deepcopy(base)
    cand["run_context"]["tns"]["current"] = False

    assert any("TNS" in flag for flag in compare_snapshots(base, cand).flags)


def test_different_sample_breaks_comparability():
    base = _snapshot([_match(1)])
    cand = copy.deepcopy(base)
    cand["run_context"]["sample"]["digest"] = "other"

    assert any("sample" in flag for flag in compare_snapshots(base, cand).flags)


def test_match_only_in_one_snapshot():
    result = compare_snapshots(
        _snapshot([_match(1)]), _snapshot([_match(1), _match(2)])
    )

    [group] = result.groups
    assert group.kind == MATCH_ONLY_IN_CANDIDATE
    assert group.catalog == "cat_a"
    assert group.count == 1

    reverse = compare_snapshots(
        _snapshot([_match(1), _match(2)]), _snapshot([_match(1)])
    )
    assert [g.kind for g in reverse.groups] == [MATCH_ONLY_IN_BASELINE]


def test_matched_source_changed():
    result = compare_snapshots(
        _snapshot([_match(1)]), _snapshot([_match(1, source_id="src-2")])
    )
    assert [g.kind for g in result.groups] == [SOURCE_CHANGED]


def test_numeric_value_change_reports_largest_difference():
    result = compare_snapshots(
        _snapshot([_match(1, mag=18.2), _match(2, mag=19.0)]),
        _snapshot([_match(1, mag=18.200001), _match(2, mag=19.5)]),
    )

    [group] = result.groups
    assert group.kind == VALUE_CHANGED
    assert group.field == "catalog_payload.mag"
    assert group.count == 2
    assert group.max_abs_diff == pytest.approx(0.5)


def test_int_to_float_and_null_to_missing_are_type_changes():
    base = _snapshot([_match(1)])
    cand = copy.deepcopy(base)
    cand["matches"][0]["published_payload"]["lsst_diaObject_diaObjectId"] = 1.0
    del cand["matches"][0]["published_payload"]["tns"]

    result = compare_snapshots(base, cand)

    fields = {(g.kind, g.field) for g in result.groups}
    assert (TYPE_CHANGED, "lsst_diaObject_diaObjectId") in fields
    assert (TYPE_CHANGED, "tns") in fields or (TNS_CHANGED, "tns") in fields


def test_many_differences_on_one_field_form_one_group():
    base = _snapshot([_match(i, mag=10.0) for i in range(1, 401)])
    cand = _snapshot([_match(i, mag=10.5) for i in range(1, 401)])

    result = compare_snapshots(base, cand)
    report = render_report(result, "a.json", "b.json")

    assert len(result.groups) == 1
    assert result.groups[0].count == 400
    assert report.count("| 1") < 20


def test_version_differences_are_listed_in_context():
    base = _snapshot([_match(1)])
    cand = copy.deepcopy(base)
    cand["run_context"]["versions"]["client"]["pandas"] = "3.0.6"

    result = compare_snapshots(base, cand)

    assert any(
        d.field == "versions.client.pandas" and d.candidate == "3.0.6"
        for d in result.context_differences
    )
    assert result.flags == []


def test_compare_command_writes_report(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_snapshot([_match(1)])))
    b.write_text(json.dumps(_snapshot([_match(1, mag=20.0)])))
    out = tmp_path / "report.md"

    call_command("replay_compare", str(a), str(b), "--output", str(out))

    report = out.read_text()
    assert "catalog_payload.mag" in report
    assert "Comparability" in report
