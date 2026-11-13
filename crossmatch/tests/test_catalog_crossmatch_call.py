"""crossmatch_alerts asks LSDB for the neighbors a catalog's match mode needs,
and an all-within-radius catalog keeps at most its cap per alert."""

from unittest.mock import MagicMock

import pandas as pd
import pytest
from django.test import override_settings

import matching.catalog as catalog_mod
from matching.catalog import crossmatch_alerts


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    catalog_mod._catalog_cache.clear()
    yield
    catalog_mod._catalog_cache.clear()


def _cfg(**extra):
    return {
        "name": "t",
        "hats_url": "x",
        "source_id_column": "source_id",
        "ra_column": "ra",
        "dec_column": "dec",
        "payload_columns": [],
        **extra,
    }


def _alerts_returning(frame):
    alerts = MagicMock()
    alerts.crossmatch.return_value.compute.return_value = frame
    return alerts


@pytest.fixture
def opened(monkeypatch):
    cat = MagicMock()
    cat.columns = ["source_id", "ra", "dec"]
    monkeypatch.setattr(catalog_mod.lsdb, "open_catalog", lambda *a, **k: cat)
    return cat


def _rows(spec):
    """Match rows from (diaObjectId, source_id, distance) tuples."""
    return pd.DataFrame(
        spec, columns=["lsst_diaObject_diaObjectId", "source_id", "_dist_arcsec"]
    )


@override_settings(CROSSMATCH_RADIUS_ARCSEC=1.0)
def test_nearest_catalog_asks_for_one_neighbor(opened):
    alerts = _alerts_returning(_rows([(1, "a", 0.5)]))
    crossmatch_alerts(alerts, _cfg(match_mode="nearest"))
    kwargs = alerts.crossmatch.call_args.kwargs
    assert alerts.crossmatch.call_args.args == (opened,)
    assert kwargs["n_neighbors"] == 1
    assert kwargs["radius_arcsec"] == 1.0


def test_entry_without_match_mode_asks_for_one_neighbor(opened):
    alerts = _alerts_returning(_rows([]))
    crossmatch_alerts(alerts, _cfg())
    assert alerts.crossmatch.call_args.kwargs["n_neighbors"] == 1


def test_all_catalog_asks_for_one_more_than_its_cap(opened):
    alerts = _alerts_returning(_rows([]))
    crossmatch_alerts(alerts, _cfg(match_mode="all", max_matches=10))
    assert alerts.crossmatch.call_args.kwargs["n_neighbors"] == 11


def test_all_catalog_keeps_the_nearest_up_to_its_cap(opened, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        catalog_mod.logger, "warning", lambda event, **kw: warnings.append((event, kw))
    )
    # Alert 1 has 12 sources (distances 1.1, 1.0, ... 0.0 shuffled); alert 2 has 2.
    many = [(1, f"s{i}", round(0.1 * i, 1)) for i in range(11, -1, -1)]
    alerts = _alerts_returning(_rows(many + [(2, "x", 0.3), (2, "y", 0.2)]))

    result = crossmatch_alerts(alerts, _cfg(match_mode="all", max_matches=10))

    kept = result[result["lsst_diaObject_diaObjectId"] == 1]
    assert sorted(kept["source_id"]) == sorted(f"s{i}" for i in range(10))
    assert len(result[result["lsst_diaObject_diaObjectId"] == 2]) == 2
    assert warnings == [
        ("crossmatch_match_cap_reached", {"catalog": "t", "max_matches": 10, "alerts": 1})
    ]


def test_all_catalog_under_its_cap_is_returned_unchanged(opened, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        catalog_mod.logger, "warning", lambda event, **kw: warnings.append((event, kw))
    )
    frame = _rows([(1, "a", 0.4), (1, "b", 0.1), (2, "c", 0.9)])
    alerts = _alerts_returning(frame)

    result = crossmatch_alerts(alerts, _cfg(match_mode="all", max_matches=10))

    assert sorted(result["source_id"]) == ["a", "b", "c"]
    assert warnings == []
