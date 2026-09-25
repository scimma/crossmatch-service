"""R6: build_catalog_payload coerces numpy/pandas scalars and null sentinels to
JSON-native values (no NaN token, no non-serializable types), lowercases keys,
and keeps a stable key set.

Also covers build_published_payload's coverage keys (R4): catalogs_skipped is a
sorted list and partial is true iff any catalog was skipped."""

import json

import numpy as np
import pandas as pd
import pytest

from matching.payload import build_catalog_payload, build_published_payload


def test_coerces_numpy_and_nulls_to_json_native():
    values = {
        "MAG_G": np.int32(17),
        "MAG_R": np.float64(18.5),
        "FLAG": np.bool_(True),
        "MISS_NONE": None,
        "MISS_NAN": np.nan,
        "MISS_NAT": pd.NaT,
        "MISS_PDNA": pd.NA,
    }
    cols = list(values.keys())

    out = build_catalog_payload(values, cols)

    json.dumps(out)  # must not raise (no numpy types, no NaN token)
    assert out["mag_g"] == 17 and isinstance(out["mag_g"], int)
    assert out["mag_r"] == 18.5 and isinstance(out["mag_r"], float)
    assert out["flag"] is True
    for k in ("miss_none", "miss_nan", "miss_nat", "miss_pdna"):
        assert out[k] is None
    assert set(out.keys()) == {c.lower() for c in cols}


def _published(catalogs_skipped=None):
    return build_published_payload(
        9_000_000_001,
        180.0,
        -30.0,
        "gaia_dr3",
        "src-1",
        0.5,
        {"phot_g_mean_mag": 18.2},
        catalogs_skipped=catalogs_skipped,
    )


def test_published_payload_full_coverage_by_default():
    # No skipped catalogs -> covered every catalog: partial False, empty list.
    out = _published()

    json.dumps(out)  # published as JSON over Hopskotch; must not raise
    assert out["partial"] is False
    assert out["catalogs_skipped"] == []


def test_published_payload_marks_partial_and_sorts_skipped():
    # A skip stamps partial True and normalizes catalogs_skipped to a sorted list.
    out = _published(catalogs_skipped={"skymapper_dr4", "des_y6_gold"})

    assert out["partial"] is True
    assert out["catalogs_skipped"] == ["des_y6_gold", "skymapper_dr4"]


# --- TNS block + enrichment indicator (plan U5, R5/R6/R7) --------------------

from datetime import datetime, timezone

_EPOCH = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def test_tns_block_absent_when_no_association():
    """No TNS counterpart -> no `tns` key, but the indicator is still present."""
    out = _published()
    assert "tns" not in out
    assert out["tns_checked"] is False
    assert out["tns_snapshot_epoch"] is None


def test_tns_block_present_with_all_fields():
    tns = {
        "objid": np.int64(4242),
        "name": "2024xyz",
        "url": "https://www.wis-tns.org/object/2024xyz",
        "classification": "SN Ia",
        "redshift": np.float64(0.05),
        "separation_arcsec": np.float64(0.3),
    }
    out = build_published_payload(
        9_000_000_001, 180.0, -30.0, "gaia_dr3", "src-1", 0.5,
        {"phot_g_mean_mag": 18.2},
        tns=tns, tns_checked=True, tns_snapshot_epoch=_EPOCH,
    )
    json.dumps(out)  # published as JSON; must not raise
    assert out["tns_checked"] is True
    assert out["tns_snapshot_epoch"] == "2026-08-14T12:00:00+00:00"
    block = out["tns"]
    assert block["objid"] == 4242 and isinstance(block["objid"], int)
    assert block["name"] == "2024xyz"
    assert block["url"] == "https://www.wis-tns.org/object/2024xyz"
    assert block["classification"] == "SN Ia"
    assert block["redshift"] == 0.05 and isinstance(block["redshift"], float)
    assert block["separation_arcsec"] == pytest.approx(0.3)


def test_tns_block_null_classification_and_redshift():
    """Covers AE5: a named object with no classification/redshift still emits a block."""
    tns = {
        "objid": 7,
        "name": "2024aaa",
        "url": "https://www.wis-tns.org/object/2024aaa",
        "classification": None,
        "redshift": np.float64(np.nan),
        "separation_arcsec": 0.9,
    }
    out = build_published_payload(
        9_000_000_002, 10.0, 10.0, "gaia_dr3", "src-2", 0.5, {},
        tns=tns, tns_checked=True, tns_snapshot_epoch=_EPOCH,
    )
    json.dumps(out)  # NaN redshift must render as null, not a NaN token
    assert out["tns"]["classification"] is None
    assert out["tns"]["redshift"] is None


# --- pandas 3 value shapes (lsdb 0.11 upgrade plan U2) ------------------------
#
# pandas 3 loads strings as a StringDtype whose missing value is NaN, keeps
# nullable integers as Int64 (missing = pd.NA), and hands values to the payload
# builder through itertuples exactly as the crossmatch compute step reads rows.
# These pin the published values so a pandas change cannot alter them silently.


def _row_values(frame):
    """Values of the first row, read the way the compute step reads them."""
    row = next(frame.itertuples(index=False))
    return {col: getattr(row, col) for col in frame.columns}


def test_pandas3_string_dtype_values_publish_as_plain_strings():
    frame = pd.DataFrame({"name": ["Gaia DR3 123"], "band": ["g"]})
    assert str(frame["name"].dtype) != "object"

    out = build_catalog_payload(_row_values(frame), ["name", "band"])

    assert out == {"name": "Gaia DR3 123", "band": "g"}
    assert type(out["name"]) is str


def test_pandas3_missing_string_publishes_null():
    frame = pd.DataFrame({"name": [None], "band": ["r"]})

    out = build_catalog_payload(_row_values(frame), ["name", "band"])

    assert out == {"name": None, "band": "r"}
    assert json.loads(json.dumps(out, allow_nan=False)) == out


def test_pandas3_nullable_integer_and_float_nulls_publish_null():
    frame = pd.DataFrame(
        {
            "flags": pd.array([None], dtype="Int64"),
            "mag": [np.nan],
            "count": pd.array([7], dtype="Int64"),
        }
    )

    out = build_catalog_payload(_row_values(frame), ["flags", "mag", "count"])

    assert out == {"flags": None, "mag": None, "count": 7}
    assert type(out["count"]) is int


def test_pandas3_numpy_scalars_from_frame_rows_are_json_native():
    frame = pd.DataFrame(
        {
            "n": np.array([5], dtype=np.int64),
            "mag": np.array([18.25], dtype=np.float32),
            "ok": np.array([True]),
        }
    )

    out = build_catalog_payload(_row_values(frame), ["n", "mag", "ok"])

    assert out == {"n": 5, "mag": pytest.approx(18.25), "ok": True}
    assert (type(out["n"]), type(out["mag"]), type(out["ok"])) == (int, float, bool)


def test_pandas3_large_dia_object_id_survives_frame_rows_exactly():
    big = 2**62 + 12345
    frame = pd.DataFrame({"lsst_diaObject_diaObjectId": np.array([big], dtype=np.int64)})
    row = next(frame.itertuples(index=False))

    payload = build_published_payload(
        row.lsst_diaObject_diaObjectId, 1.0, 2.0, "cat", "src", 0.1, {}
    )

    assert payload["diaObjectId"] == big
    assert type(payload["diaObjectId"]) is int


def test_pyarrow_backed_columns_publish_json_native():
    # LSDB builds catalogs with pyarrow-backed types, so .compute() results can
    # carry ArrowDtype columns; their row values and nulls must publish cleanly.
    pa = pytest.importorskip("pyarrow")
    frame = pd.DataFrame(
        {
            "name": pd.array(["obj-1"], dtype=pd.ArrowDtype(pa.string())),
            "mag": pd.array([None], dtype=pd.ArrowDtype(pa.float64())),
            "n": pd.array([3], dtype=pd.ArrowDtype(pa.int64())),
            "ok": pd.array([False], dtype=pd.ArrowDtype(pa.bool_())),
        }
    )

    out = build_catalog_payload(_row_values(frame), ["name", "mag", "n", "ok"])

    assert out == {"name": "obj-1", "mag": None, "n": 3, "ok": False}
    assert [type(out[k]) for k in ("name", "n", "ok")] == [str, int, bool]
    assert json.loads(json.dumps(out, allow_nan=False)) == out
