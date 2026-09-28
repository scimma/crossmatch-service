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
