"""Standalone checks for crossmatch.matching.payload.build_catalog_payload.

The payload helper is covered by the pytest suite (``crossmatch/tests/test_payload.py``);
this is a dependency-light standalone check in the style of
``scripts/dump_catalog_columns.py`` for verifying the coercion without bringing up
the app. Run from the repo root in an environment with numpy and pandas::

    python scripts/check_payload.py

Exits non-zero on the first failed assertion. Loads ``payload.py`` by file path
so it does not import the Django ``crossmatch`` package.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

_MODULE_PATH = Path(__file__).resolve().parents[1] / "crossmatch" / "matching" / "payload.py"
_spec = importlib.util.spec_from_file_location("payload_under_test", _MODULE_PATH)
_payload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_payload)
build_catalog_payload = _payload.build_catalog_payload
build_published_payload = _payload.build_published_payload


def check(label, cond):
    if not cond:
        raise AssertionError(label)
    print(f"  ok: {label}")


def main() -> int:
    # 1. Happy path: numpy float -> lowercased key, native float value.
    out = build_catalog_payload({"WAVG_MAG_PSF_G": np.float64(21.3)}, ["WAVG_MAG_PSF_G"])
    check("happy: key lowercased", out == {"wavg_mag_psf_g": 21.3})
    check("happy: value is python float", type(out["wavg_mag_psf_g"]) is float)

    # 2. Case flip + J2000 suffix preserved (already-lowercase unchanged).
    out = build_catalog_payload(
        {"EXT_MASH": np.int16(4), "raj2000": np.float64(10.5)},
        ["EXT_MASH", "raj2000"],
    )
    check("case: UPPERCASE -> lowercase", "ext_mash" in out)
    check("case: raj2000 suffix preserved", "raj2000" in out and out["raj2000"] == 10.5)

    # 3. NaN -> None, key still present.
    out = build_catalog_payload({"DNF_Z": np.float64(np.nan)}, ["DNF_Z"])
    check("nan: key present", "dnf_z" in out)
    check("nan: value is None", out["dnf_z"] is None)

    # 4. numpy scalar coercion to python natives (assert exact type).
    out = build_catalog_payload(
        {"i64": np.int64(7), "f64": np.float64(1.5), "b": np.bool_(True)},
        ["i64", "f64", "b"],
    )
    check("coerce: np.int64 -> int", type(out["i64"]) is int and out["i64"] == 7)
    check("coerce: np.float64 -> float", type(out["f64"]) is float and out["f64"] == 1.5)
    check("coerce: np.bool_ -> bool", type(out["b"]) is bool and out["b"] is True)

    # int width variety (DELVE flags are int16/int32; SkyMapper int64).
    out = build_catalog_payload(
        {"a": np.int16(1), "b": np.int32(2), "c": np.int64(3)}, ["a", "b", "c"]
    )
    check("coerce: int16/int32/int64 -> int", all(type(out[k]) is int for k in ("a", "b", "c")))

    # 5. Stable key set: a column absent from values still appears as None.
    out = build_catalog_payload({"DNF_Z": np.float64(0.1)}, ["DNF_Z", "DNF_ZSIGMA"])
    check("stable: missing column present as None", out == {"dnf_z": 0.1, "dnf_zsigma": None})

    # 6. Empty payload_columns -> {}.
    check("empty: {} for no columns", build_catalog_payload({"X": 1}, []) == {})

    # 7. Integer flag stays int, not coerced to float.
    out = build_catalog_payload({"BDF_FLAGS": np.int64(16)}, ["BDF_FLAGS"])
    check("flag: bdf_flags stays int", type(out["bdf_flags"]) is int and out["bdf_flags"] == 16)

    # 8. pandas nullable-int missing (pd.NA) -> None.
    out = build_catalog_payload({"FLAGS_GOLD": pd.NA}, ["FLAGS_GOLD"])
    check("pd.NA: -> None", out["flags_gold"] is None)

    # 9. Python-native NaN (not just numpy) -> None.
    out = build_catalog_payload({"x": float("nan")}, ["x"])
    check("py nan: -> None", out["x"] is None)

    # 10. Whole result is JSON-serializable (no numpy/NaN leakage).
    out = build_catalog_payload(
        {
            "WAVG_MAG_PSF_G": np.float64(21.3),
            "BDF_FLAGS": np.int64(0),
            "DNF_Z": np.float64(np.nan),
            "class_star": np.float64(0.98),
        },
        ["WAVG_MAG_PSF_G", "BDF_FLAGS", "DNF_Z", "class_star"],
    )
    encoded = json.dumps(out)  # raises TypeError if a numpy scalar leaked
    check("json: serializable", isinstance(encoded, str))
    check("json: NaN rendered as null", json.loads(encoded)["dnf_z"] is None)

    # 11. TNS block: numpy objid/redshift/separation coerce; datetime epoch -> ISO;
    #     NaN redshift -> null; whole published payload is JSON-serializable.
    from datetime import datetime, timezone

    epoch = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    published = build_published_payload(
        9_000_000_001,
        180.0,
        -30.0,
        "gaia_dr3",
        "src-1",
        0.5,
        {"phot_g_mean_mag": np.float64(18.2)},
        tns={
            "objid": np.int64(4242),
            "name": "2024xyz",
            "url": "https://www.wis-tns.org/object/2024xyz",
            "classification": "SN Ia",
            "redshift": np.float64(np.nan),
            "separation_arcsec": np.float64(0.3),
        },
        tns_checked=True,
        tns_snapshot_epoch=epoch,
    )
    encoded = json.dumps(published)  # raises if a numpy/datetime leaked
    reloaded = json.loads(encoded)
    check("tns: published payload JSON-serializable", isinstance(encoded, str))
    check("tns: objid coerced to int", type(published["tns"]["objid"]) is int)
    check("tns: NaN redshift -> null", reloaded["tns"]["redshift"] is None)
    check("tns: epoch rendered as ISO string", reloaded["tns_snapshot_epoch"] == epoch.isoformat())
    check("tns: indicator present", reloaded["tns_checked"] is True)

    # 12. No association: no tns block, but the indicator is still present.
    no_match = build_published_payload(
        9_000_000_002, 10.0, 10.0, "gaia_dr3", "src-2", 0.5, {}, tns_checked=True
    )
    check("tns: absent block when no association", "tns" not in no_match)
    check("tns: indicator present without block", no_match["tns_checked"] is True)

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
