"""R7: _get_catalog validates requested columns up front — a column colliding
with an alert column, or one missing from the catalog schema, fails loud with a
clear ValueError instead of a cryptic error deep in .compute()."""

from unittest.mock import MagicMock

import pytest

import matching.catalog as catalog_mod
from matching.catalog import _get_catalog


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    # The module-level cache short-circuits validation; reset it per test.
    catalog_mod._catalog_cache.clear()
    yield
    catalog_mod._catalog_cache.clear()


def _cfg(payload_columns):
    return {
        "name": "t",
        "hats_url": "x",
        "source_id_column": "source_id",
        "ra_column": "ra",
        "dec_column": "dec",
        "payload_columns": payload_columns,
    }


def test_collision_with_alert_column_raises():
    with pytest.raises(ValueError, match="collide"):
        _get_catalog(_cfg(["ra_deg"]))  # ra_deg is a reserved alert column


def test_unknown_column_raises(monkeypatch):
    cat = MagicMock()
    cat.columns = ["source_id", "ra", "dec"]  # 'mag' absent
    monkeypatch.setattr(catalog_mod.lsdb, "open_catalog", lambda *a, **k: cat)

    with pytest.raises(ValueError, match="not found"):
        _get_catalog(_cfg(["mag"]))


def test_valid_columns_returns_catalog(monkeypatch):
    cat = MagicMock()
    cat.columns = ["source_id", "ra", "dec", "mag"]
    monkeypatch.setattr(catalog_mod.lsdb, "open_catalog", lambda *a, **k: cat)

    assert _get_catalog(_cfg(["mag"])) is cat


def test_skip_point_map_opens_without_reading_the_point_map(monkeypatch):
    """A skip_point_map catalog is read with read_moc=False, for both the schema
    check and the real open, and never through lsdb.open_catalog."""
    hc_reads = []
    opened = []
    cat = MagicMock()
    cat.columns = ["source_id", "ra", "dec", "mag"]

    def fake_read_hats(url, **kwargs):
        hc_reads.append((url, kwargs))
        return f"hc:{url}"

    def fake_read_dataset(hc_catalog, *, columns):
        opened.append((hc_catalog, columns))
        return cat

    def no_open_catalog(*a, **k):
        raise AssertionError("lsdb.open_catalog would read point_map.fits")

    monkeypatch.setattr(catalog_mod.hats, "read_hats", fake_read_hats)
    monkeypatch.setattr(catalog_mod, "_read_dataset", fake_read_dataset)
    monkeypatch.setattr(catalog_mod.lsdb, "open_catalog", no_open_catalog)

    assert _get_catalog({**_cfg(["mag"]), "skip_point_map": True}) is cat
    assert hc_reads == [("x", {"read_moc": False})] * 2
    assert opened == [("hc:x", "all"), ("hc:x", ["source_id", "ra", "dec", "mag"])]


def test_lsdb_still_offers_the_dataset_reader_skip_point_map_relies_on():
    """_open_catalog calls lsdb's private _read_dataset; fail on an lsdb upgrade
    that renames it or drops its columns keyword, not at the first batch."""
    import inspect

    from lsdb.loaders.hats import read_hats

    params = inspect.signature(read_hats._read_dataset).parameters
    assert "columns" in params
    assert params["columns"].kind is inspect.Parameter.KEYWORD_ONLY


def test_skymapper_skips_its_point_map_and_declares_the_skymap_order():
    """SkyMapper's coverage comes from its order-6 skymap, so the declared
    footprint order must not claim finer resolution than that."""
    sm = next(c for c in settings.CROSSMATCH_CATALOGS if c["name"] == "skymapper_dr4")
    assert sm["skip_point_map"] is True
    assert sm["footprint_moc_order"] == 6


# --- filter_columns (U6, KTD9 step 1) -----------------------------------------

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from project.settings import _validate_filter_columns

APP_ROOT = Path(__file__).resolve().parent.parent


def _filter_cfg(filter_columns=None, derived=None):
    cfg = {
        "name": "t",
        "payload_columns": ["parallax", "parallax_error", "DNF_Z"],
        "filter_columns": filter_columns if filter_columns is not None else {},
    }
    if derived is not None:
        cfg["derived_filter_columns"] = derived
    return cfg


def test_configured_filter_columns_are_payload_columns_with_units():
    for cat in settings.CROSSMATCH_CATALOGS:
        payload = set(cat["payload_columns"])
        for column, unit in cat.get("filter_columns", {}).items():
            assert column in payload, (cat["name"], column)
            assert isinstance(unit, str) and unit.strip(), (cat["name"], column)


def test_gaia_declares_the_derived_parallax_significance():
    gaia = next(c for c in settings.CROSSMATCH_CATALOGS if c["name"] == "gaia_dr3")
    derived = gaia["derived_filter_columns"]["parallax_over_error"]
    assert (derived["numerator"], derived["denominator"]) == ("parallax", "parallax_error")


def test_valid_filter_columns_pass():
    _validate_filter_columns([_filter_cfg(
        {"parallax": "mas", "DNF_Z": "dimensionless"},
        {"parallax_over_error": {
            "numerator": "parallax", "denominator": "parallax_error",
            "unit": "dimensionless",
        }},
    )])


def test_filter_column_missing_from_payload_columns_raises():
    with pytest.raises(ImproperlyConfigured, match="ruwe"):
        _validate_filter_columns([_filter_cfg({"ruwe": "dimensionless"})])


def test_filter_column_in_the_wrong_case_raises():
    with pytest.raises(ImproperlyConfigured, match="dnf_z"):
        _validate_filter_columns([_filter_cfg({"dnf_z": "dimensionless"})])


@pytest.mark.parametrize("unit", ["", "  ", None, 3])
def test_filter_column_without_a_unit_raises(unit):
    with pytest.raises(ImproperlyConfigured, match="unit"):
        _validate_filter_columns([_filter_cfg({"parallax": unit})])


def test_derived_filter_operand_missing_from_payload_columns_raises():
    with pytest.raises(ImproperlyConfigured, match="pmra"):
        _validate_filter_columns([_filter_cfg(derived={"x": {
            "numerator": "pmra", "denominator": "parallax_error", "unit": "u",
        }})])


def test_derived_filter_colliding_with_a_filter_column_raises():
    with pytest.raises(ImproperlyConfigured, match="(?i)collides"):
        _validate_filter_columns([_filter_cfg({"parallax": "mas"}, {"PARALLAX": {
            "numerator": "parallax", "denominator": "parallax_error", "unit": "u",
        }})])


def test_filter_column_missing_from_payload_columns_raises_at_import():
    """The settings module refuses to import (a subprocess keeps this honest)."""
    code = (
        "import pathlib, sys, types\n"
        "path = pathlib.Path('project/settings.py').resolve()\n"
        "src = path.read_text()\n"
        "old = \"'ruwe': 'dimensionless',\"\n"
        "assert src.count(old) == 1, 'filter declaration not found'\n"
        "src = src.replace(old, \"'not_a_payload_column': 'dimensionless',\")\n"
        "mod = types.ModuleType('project.settings')\n"
        "mod.__file__ = str(path)\n"
        "exec(compile(src, str(path), 'exec'), mod.__dict__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=APP_ROOT, env=dict(os.environ),
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "not_a_payload_column" in result.stderr
    assert "gaia_dr3" in result.stderr


# --- match_mode / max_matches --------------------------------------------------

from project.settings import _validate_match_modes


def _mode_cfg(**extra):
    return {"name": "t", **extra}


def test_configured_catalogs_carry_a_match_mode():
    for cat in settings.CROSSMATCH_CATALOGS:
        assert cat["match_mode"] in ("nearest", "all"), cat["name"]


def test_entry_without_match_mode_validates_as_nearest():
    _validate_match_modes([_mode_cfg()])


def test_all_mode_with_max_matches_validates():
    _validate_match_modes([_mode_cfg(match_mode="all", max_matches=10)])


def test_all_mode_without_max_matches_raises():
    with pytest.raises(ImproperlyConfigured, match="'t'.*max_matches"):
        _validate_match_modes([_mode_cfg(match_mode="all")])


@pytest.mark.parametrize("max_matches", [True, 1, 0, "10", 2.5])
def test_all_mode_with_invalid_max_matches_raises(max_matches):
    with pytest.raises(ImproperlyConfigured, match="max_matches"):
        _validate_match_modes([_mode_cfg(match_mode="all", max_matches=max_matches)])


def test_unknown_match_mode_raises():
    with pytest.raises(ImproperlyConfigured, match="match_mode"):
        _validate_match_modes([_mode_cfg(match_mode="every")])


def test_max_matches_on_a_nearest_entry_raises():
    with pytest.raises(ImproperlyConfigured, match="max_matches"):
        _validate_match_modes([_mode_cfg(match_mode="nearest", max_matches=5)])
