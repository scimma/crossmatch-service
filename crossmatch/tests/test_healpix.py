"""U2 / R5, R6: HEALPix spatial helper — stable point index, cone-to-ipix ranges
correct across RA wraparound (AE4) and at the poles, and an exact fine-filter."""

from core.healpix import (
    HEALPIX_ORDER,
    angular_separation_arcsec,
    cone_ipix_ranges,
    radec_to_ipix,
    radec_to_ipix_array,
)


def _in_ranges(ipix, ranges):
    return any(lo <= ipix <= hi for lo, hi in ranges)


def test_order_is_16():
    assert HEALPIX_ORDER == 16


def test_point_index_is_stable_and_local():
    # A fixed coordinate maps to a fixed, reproducible pixel.
    first = radec_to_ipix(0.0, 0.0)
    assert radec_to_ipix(0.0, 0.0) == first
    assert isinstance(first, int)
    # Two points closer than one order-16 pixel (~3.2 arcsec) share an index.
    assert radec_to_ipix(10.0, 20.0) == radec_to_ipix(10.0 + 0.0001, 20.0)
    # Two widely separated points differ.
    assert radec_to_ipix(10.0, 20.0) != radec_to_ipix(200.0, -40.0)


def test_point_index_array_matches_scalar():
    ras = [0.0, 180.0, 359.99]
    decs = [0.0, -30.0, 0.0]
    batch = radec_to_ipix_array(ras, decs)
    assert batch == [radec_to_ipix(r, d) for r, d in zip(ras, decs)]
    assert all(isinstance(x, int) for x in batch)


def test_invalid_coordinates_return_none():
    # Out-of-range declination and non-finite coordinates must not reach
    # cdshealpix (which raises / panics); they degrade to None instead.
    assert radec_to_ipix(10.0, 95.0) is None
    assert radec_to_ipix(10.0, -95.0) is None
    assert radec_to_ipix(float("nan"), 10.0) is None
    assert radec_to_ipix(10.0, float("nan")) is None


def test_array_isolates_invalid_rows():
    # A bad row yields None without aborting the batch or shifting other rows.
    result = radec_to_ipix_array([10.0, 20.0, 30.0], [95.0, 30.0, float("nan")])
    assert result == [None, radec_to_ipix(20.0, 30.0), None]


def test_cone_ranges_cover_ra_wraparound():
    # Covers AE4: a cone centered near RA 0 matches objects at RA 359.9 and 0.1.
    # 0.1 deg == 360 arcsec, so the cone must be at least that wide.
    ranges = cone_ipix_ranges(0.0, 0.0, 400.0)
    assert _in_ranges(radec_to_ipix(359.9, 0.0), ranges)
    assert _in_ranges(radec_to_ipix(0.1, 0.0), ranges)
    assert _in_ranges(radec_to_ipix(0.0, 0.0), ranges)


def test_cone_ranges_are_sorted_and_nonoverlapping():
    ranges = cone_ipix_ranges(45.0, 45.0, 300.0)
    assert ranges == sorted(ranges)
    for (lo, hi) in ranges:
        assert lo <= hi
    for prev, nxt in zip(ranges, ranges[1:]):
        assert prev[1] + 1 < nxt[0]  # contiguous pixels were merged into one range


def test_fine_filter_is_exact_at_the_boundary():
    # angular_separation_arcsec is the exact filter applied after the range pre-filter.
    radius = 60.0
    # A point just inside the radius passes; one just outside fails.
    inside = angular_separation_arcsec(0.0, 0.0, 0.0, 59.0 / 3600.0)
    outside = angular_separation_arcsec(0.0, 0.0, 0.0, 61.0 / 3600.0)
    assert inside <= radius
    assert outside > radius
    # Symmetry and zero self-separation.
    assert angular_separation_arcsec(10.0, -20.0, 10.0, -20.0) == 0.0


def test_cone_near_pole_returns_valid_ranges():
    ranges = cone_ipix_ranges(0.0, 89.9, 120.0)
    assert ranges  # non-empty, no error
    assert _in_ranges(radec_to_ipix(0.0, 89.9), ranges)


# --- U5 / KTD12.5: coarse-depth cone cover, widened to order 16 ---

import math  # noqa: E402

import astropy.units as u  # noqa: E402
from cdshealpix import nested  # noqa: E402

from core.healpix import MAX_COVER_PIXELS, cone_cover_depth, cone_cover_ranges  # noqa: E402


def _cover_pixel_count(ra, dec, radius_arcsec, depth):
    ipix, _depths, _full = nested.cone_search(
        lon=ra * u.deg, lat=dec * u.deg, radius=radius_arcsec * u.arcsec,
        depth=depth, flat=True,
    )
    return len(set(int(p) for p in ipix))


def _points_on_circle(ra, dec, radius_arcsec, n=36):
    """Points just inside the cone boundary, all around it."""
    r = math.radians(radius_arcsec * 0.999 / 3600.0)
    d0, a0 = math.radians(dec), math.radians(ra)
    points = []
    for k in range(n):
        bearing = 2 * math.pi * k / n
        d = math.asin(math.sin(d0) * math.cos(r) + math.cos(d0) * math.sin(r) * math.cos(bearing))
        a = a0 + math.atan2(
            math.sin(bearing) * math.sin(r) * math.cos(d0),
            math.cos(r) - math.sin(d0) * math.sin(d),
        )
        points.append((math.degrees(a) % 360.0, math.degrees(d)))
    return points


def test_cover_depth_is_the_deepest_with_at_most_nine_pixels():
    for ra, dec, radius in [(45.0, 45.0, 60.0), (10.0, -30.0, 2.0), (200.0, 5.0, 30.0)]:
        depth = cone_cover_depth(ra, dec, radius)
        assert _cover_pixel_count(ra, dec, radius, depth) <= MAX_COVER_PIXELS
        if depth < HEALPIX_ORDER:
            assert _cover_pixel_count(ra, dec, radius, depth + 1) > MAX_COVER_PIXELS


def test_small_cone_covers_at_the_stored_order():
    assert cone_cover_depth(10.0, -30.0, 0.5) == HEALPIX_ORDER


def test_cover_ranges_are_order_16_and_contain_every_point_in_the_cone():
    for ra, dec, radius in [(45.0, 45.0, 60.0), (0.0, 0.0, 30.0), (0.0, 89.99, 60.0),
                            (123.0, -89.995, 45.0)]:
        ranges = cone_cover_ranges(ra, dec, radius)
        assert ranges == sorted(ranges)
        assert len(ranges) <= MAX_COVER_PIXELS
        assert all(0 <= lo <= hi < 12 * 4 ** HEALPIX_ORDER for lo, hi in ranges)
        assert _in_ranges(radec_to_ipix(ra, dec), ranges)
        for pra, pdec in _points_on_circle(ra, dec, radius):
            assert _in_ranges(radec_to_ipix(pra, pdec), ranges), (ra, dec, pra, pdec)


def test_cover_ranges_span_ra_wraparound():
    ranges = cone_cover_ranges(0.0, 10.0, 20.0)
    assert _in_ranges(radec_to_ipix(359.998, 10.0), ranges)
    assert _in_ranges(radec_to_ipix(0.002, 10.0), ranges)
