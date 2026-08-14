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
