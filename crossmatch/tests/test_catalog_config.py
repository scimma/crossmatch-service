"""Catalog source URLs: DES and DELVE read from their public S3 mirrors; SkyMapper
stays on data.lsdb.io HTTPS (no S3 mirror); Gaia is unchanged.

Guards the reliability fix that moved DES Y6 Gold and DELVE DR3 Gold off the
degraded data.lsdb.io CDN onto the anonymous stpubdata S3 mirrors (R1, R4).
"""

from django.conf import settings


def _hats_url(name: str) -> str:
    return next(
        c["hats_url"] for c in settings.CROSSMATCH_CATALOGS if c["name"] == name
    )


def test_des_reads_from_s3():
    url = _hats_url("des_y6_gold")
    assert url.startswith("s3://"), url
    assert "data.lsdb.io" not in url


def test_delve_reads_from_s3():
    url = _hats_url("delve_dr3_gold")
    assert url.startswith("s3://"), url
    assert "data.lsdb.io" not in url


def test_skymapper_stays_on_https():
    # No public S3 mirror exists for SkyMapper DR4; it stays on data.lsdb.io by
    # decision (its residual exposure is accepted; see the plan's KD3).
    assert "data.lsdb.io" in _hats_url("skymapper_dr4")


def test_gaia_unchanged():
    assert _hats_url("gaia_dr3").startswith("s3://stpubdata")


# --- Euclid DR1 MER --------------------------------------------------------------

import json
import os
import subprocess
import sys
from pathlib import Path

from matching.catalog import _ALERT_COLUMNS

APP_ROOT = Path(__file__).resolve().parent.parent
EUCLID = ("euclid_dr1_mer_deep", "euclid_dr1_mer_wide")


def _catalog(name: str) -> dict:
    return next(c for c in settings.CROSSMATCH_CATALOGS if c["name"] == name)


def _settings_in_subprocess(env: dict) -> subprocess.CompletedProcess:
    code = (
        "import json, project.settings as s; "
        "print(json.dumps([[c['name'], c['hats_url']] for c in s.CROSSMATCH_CATALOGS]))"
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=APP_ROOT,
        env={**os.environ, **env}, capture_output=True, text=True,
    )


def test_euclid_catalogs_read_irsa_s3_and_keep_every_source():
    for name in EUCLID:
        cat = _catalog(name)
        assert cat["hats_url"].startswith("s3://nasa-irsa-euclid-dr1/"), name
        assert (cat["match_mode"], cat["max_matches"]) == ("all", 10), name
        assert (cat["ra_column"], cat["dec_column"]) == ("ra", "dec"), name
        assert cat["source_id_column"] == "object_id", name


def test_euclid_payload_columns_cover_photometry_classification_and_shape():
    for name in EUCLID:
        payload = set(_catalog(name)["payload_columns"])
        assert {"ra", "dec", "flux_vis_psf", "flux_h_templfit", "point_like_prob",
                "spurious_flag", "det_quality_flag", "ellipticity", "kron_radius",
                "fwhm", "mu_max", "has_spectrum"} <= payload, name
        assert not payload & _ALERT_COLUMNS, name


def test_euclid_hats_url_can_be_overridden_from_the_environment():
    result = _settings_in_subprocess({"EUCLID_DR1_MER_WIDE_HATS_URL": "s3://elsewhere/w"})
    assert result.returncode == 0, result.stderr
    urls = dict(json.loads(result.stdout))
    assert urls["euclid_dr1_mer_wide"] == "s3://elsewhere/w"


def test_disabling_euclid_wide_leaves_it_out_and_keeps_deep():
    result = _settings_in_subprocess({"EUCLID_DR1_MER_WIDE_ENABLED": "false"})
    assert result.returncode == 0, result.stderr
    names = [name for name, _ in json.loads(result.stdout)]
    assert "euclid_dr1_mer_wide" not in names
    assert "euclid_dr1_mer_deep" in names
    assert "gaia_dr3" in names


def test_unparseable_euclid_switch_is_refused():
    result = _settings_in_subprocess({"EUCLID_DR1_MER_DEEP_ENABLED": "maybe"})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "EUCLID_DR1_MER_DEEP_ENABLED" in result.stderr
