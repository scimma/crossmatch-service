"""Tests for the in-process TNS association matcher (plan U6)."""

import pytest

from core.healpix import radec_to_ipix
from core.models import TnsObject
from matching.tns_match import cone_candidates, find_tns_match, nearest_within

ARCSEC = 1.0 / 3600.0  # one arcsecond in degrees


def _obj(objid, ra, dec, **kw):
    """An unsaved TnsObject for pure (DB-free) matcher tests."""
    return TnsObject(objid=objid, name=str(objid), ra_deg=ra, dec_deg=dec, **kw)


def test_nearest_within_single_match_reports_separation():
    center_ra, center_dec = 180.0, -30.0
    obj = _obj(1, center_ra, center_dec + 0.5 * ARCSEC, type="SN Ia", redshift=0.05)
    match = nearest_within(center_ra, center_dec, 1.0, [obj])
    assert match is not None
    assert match.objid == 1
    assert match.separation_arcsec == pytest.approx(0.5, abs=0.02)
    assert match.type == "SN Ia"


def test_nearest_within_picks_the_nearer_of_two():
    """Covers AE3: two objects within the radius -> the nearer wins."""
    ra, dec = 10.0, 10.0
    near = _obj(1, ra, dec + 0.4 * ARCSEC)
    far = _obj(2, ra, dec + 0.8 * ARCSEC)
    match = nearest_within(ra, dec, 1.0, [far, near])  # order should not matter
    assert match.objid == 1
    assert match.separation_arcsec == pytest.approx(0.4, abs=0.02)


def test_nearest_within_none_outside_radius():
    """Covers the AE2 boundary: an object just outside the radius is not a match."""
    ra, dec = 0.0, 0.0
    obj = _obj(1, ra, dec + 2.0 * ARCSEC)  # ~2 arcsec away
    assert nearest_within(ra, dec, 1.0, [obj]) is None


def test_nearest_within_invalid_coords_returns_none():
    obj = _obj(1, 10.0, 10.0)
    assert nearest_within(float("nan"), 10.0, 1.0, [obj]) is None
    assert nearest_within(10.0, 999.0, 1.0, [obj]) is None


@pytest.mark.django_db
def test_find_tns_match_queries_and_matches():
    ra, dec = 180.0, -30.0
    TnsObject.objects.create(
        objid=4242, name="2024xyz", name_prefix="SN",
        ra_deg=ra, dec_deg=dec + 0.3 * ARCSEC, type="SN Ia", redshift=0.05,
        healpix_ipix=radec_to_ipix(ra, dec + 0.3 * ARCSEC),
    )
    match = find_tns_match(ra, dec, 1.0)
    assert match is not None
    assert match.objid == 4242
    assert match.name == "2024xyz"
    assert match.separation_arcsec == pytest.approx(0.3, abs=0.05)


@pytest.mark.django_db
def test_find_tns_match_none_when_far():
    TnsObject.objects.create(
        objid=1, name="a", ra_deg=180.0, dec_deg=-30.0,
        healpix_ipix=radec_to_ipix(180.0, -30.0),
    )
    # A position well away from the only object yields no cone candidates / match.
    assert find_tns_match(12.0, 45.0, 1.0) is None


@pytest.mark.django_db
def test_cone_candidates_empty_for_invalid_coords():
    assert list(cone_candidates(float("nan"), 10.0, 1.0)) == []
