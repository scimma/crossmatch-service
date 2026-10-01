"""HEALPix spatial helpers for the alert read model.

Point indexing and cone/region range queries over the alert table's
``healpix_ipix`` column, using cdshealpix NESTED at a single fixed order so that
ingest (point -> ipix) and query (cone -> ipix ranges) stay in the same
tessellation. cdshealpix is already available as a locked transitive dependency
via ``hats`` (see the plan's KTD1), so no new dependency is introduced.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import astropy.units as u
import numpy as np
from cdshealpix import nested

# NESTED order 16 -> nside 65536, ~3.2 arcsec pixels. This value is persisted in
# every ``healpix_ipix`` (backfilled and go-forward); changing it silently mixes
# resolutions in the column and would require re-backfilling every row, so it is
# load-bearing data, not a free retune (see the plan's KTD4).
HEALPIX_ORDER = 16


def _coords_valid(ra_deg: float, dec_deg: float) -> bool:
    """True when the coordinates are safe to pass to cdshealpix.

    cdshealpix rejects a declination outside [-90, 90] with a ValueError and
    panics its native layer on NaN, so a single malformed coordinate would
    otherwise abort the alert's ingest or the whole backfill migration. Callers
    treat an invalid coordinate as "no pixel" (nullable ``healpix_ipix``) rather
    than letting it propagate — matching the repo's per-row-defensive convention.
    """
    return (
        math.isfinite(ra_deg)
        and math.isfinite(dec_deg)
        and -90.0 <= dec_deg <= 90.0
    )


def radec_to_ipix(ra_deg: float, dec_deg: float) -> int | None:
    """Return the NESTED HEALPix pixel index for one sky position.

    Args:
        ra_deg: Right ascension in degrees.
        dec_deg: Declination in degrees.

    Returns:
        The order-``HEALPIX_ORDER`` NESTED pixel index as a Python int, or
        ``None`` when the coordinate is non-finite or the declination is out of
        range (so a bad row degrades the read model instead of failing ingest).
    """
    if not _coords_valid(ra_deg, dec_deg):
        return None
    ipix = nested.lonlat_to_healpix(
        np.array([ra_deg], dtype=float) * u.deg,
        np.array([dec_deg], dtype=float) * u.deg,
        depth=HEALPIX_ORDER,
    )
    return int(ipix[0])


def radec_to_ipix_array(
    ra_deg: Sequence[float], dec_deg: Sequence[float]
) -> list[int | None]:
    """Return NESTED HEALPix pixel indices for a batch of sky positions.

    Vectorized companion to :func:`radec_to_ipix` for bulk work such as the
    existing-corpus backfill, where a per-row scalar call would pay the
    array-construction and native-call overhead once per row. Invalid rows
    (non-finite coordinate or out-of-range declination) yield ``None`` and are
    excluded from the native call so one bad row cannot abort the batch.

    Args:
        ra_deg: Sequence of right ascensions in degrees.
        dec_deg: Sequence of declinations in degrees, aligned with ``ra_deg``.

    Returns:
        A list of order-``HEALPIX_ORDER`` NESTED pixel indices (Python ints) or
        ``None`` for invalid coordinates, one per input pair, in input order.
    """
    ra = np.asarray(ra_deg, dtype=float)
    dec = np.asarray(dec_deg, dtype=float)
    valid = np.isfinite(ra) & np.isfinite(dec) & (dec >= -90.0) & (dec <= 90.0)

    result: list[int | None] = [None] * len(ra)
    if valid.any():
        ipix = nested.lonlat_to_healpix(
            ra[valid] * u.deg,
            dec[valid] * u.deg,
            depth=HEALPIX_ORDER,
        )
        for position, pixel in zip(np.nonzero(valid)[0], ipix):
            result[int(position)] = int(pixel)
    return result


def cone_ipix_ranges(
    ra_deg: float, dec_deg: float, radius_arcsec: float
) -> list[tuple[int, int]]:
    """Return contiguous NESTED pixel ranges covering a cone.

    The ranges are inclusive ``[lo, hi]`` bounds suitable for a SQL
    ``healpix_ipix BETWEEN lo AND hi`` predicate. ``cone_search`` is called with
    ``flat=True`` so every returned pixel is at order ``HEALPIX_ORDER`` (the
    multi-resolution default would mix depths, whose indices are not comparable
    to the stored order-16 values); adjacent pixels are merged into ranges. The
    cover is inclusive and may include pixels whose centers lie just outside the
    radius, so callers must apply an exact :func:`angular_separation_arcsec`
    fine-filter to the candidate rows.

    Args:
        ra_deg: Cone-center right ascension in degrees.
        dec_deg: Cone-center declination in degrees.
        radius_arcsec: Cone radius in arcseconds.

    Returns:
        A sorted list of inclusive ``(lo, hi)`` NESTED pixel ranges.
    """
    ipix_arr, _depths, _fully_covered = nested.cone_search(
        lon=ra_deg * u.deg,
        lat=dec_deg * u.deg,
        radius=radius_arcsec * u.arcsec,
        depth=HEALPIX_ORDER,
        flat=True,
    )
    pixels = np.unique(np.asarray(ipix_arr, dtype=np.int64))
    return _merge_contiguous(pixels)


#: The most pixels a cone's coarse cover may hold (KTD12 step 5). A cone is
#: covered at the deepest order where it touches at most this many pixels, so
#: a search is a handful of index range scans whatever its radius.
MAX_COVER_PIXELS = 9


def _cone_pixels(
    ra_deg: float, dec_deg: float, radius_arcsec: float, depth: int
) -> np.ndarray:
    """The sorted unique NESTED pixels at ``depth`` that a cone touches."""
    ipix_arr, _depths, _fully_covered = nested.cone_search(
        lon=ra_deg * u.deg,
        lat=dec_deg * u.deg,
        radius=radius_arcsec * u.arcsec,
        depth=depth,
        flat=True,
    )
    return np.unique(np.asarray(ipix_arr, dtype=np.int64))


def cone_cover_depth(ra_deg: float, dec_deg: float, radius_arcsec: float) -> int:
    """Return the deepest order at which a cone touches at most 9 pixels.

    The pixel count only grows with depth, so the search walks down from
    ``HEALPIX_ORDER`` and stops at the first order within
    ``MAX_COVER_PIXELS``.

    Args:
        ra_deg: Cone-center right ascension in degrees.
        dec_deg: Cone-center declination in degrees, in [-90, 90].
        radius_arcsec: Cone radius in arcseconds, positive.

    Returns:
        The cover order, in ``[0, HEALPIX_ORDER]``.
    """
    for depth in range(HEALPIX_ORDER, 0, -1):
        if len(_cone_pixels(ra_deg, dec_deg, radius_arcsec, depth)) <= MAX_COVER_PIXELS:
            return depth
    return 0


def cone_cover_ranges(
    ra_deg: float, dec_deg: float, radius_arcsec: float
) -> list[tuple[int, int]]:
    """Return the coarse cover of a cone as inclusive order-16 pixel ranges.

    The cone is covered at :func:`cone_cover_depth` (at most
    ``MAX_COVER_PIXELS`` pixels), and each coarse pixel is widened to the
    contiguous block of order-``HEALPIX_ORDER`` pixels it contains, so the
    result compares directly with the stored ``healpix_ipix`` values. Unlike
    :func:`cone_ipix_ranges` this yields at most nine ranges whatever the
    radius, at the cost of more cover candidates; callers must apply the exact
    separation filter.

    Args:
        ra_deg: Cone-center right ascension in degrees.
        dec_deg: Cone-center declination in degrees, in [-90, 90].
        radius_arcsec: Cone radius in arcseconds, positive.

    Returns:
        A sorted list of at most ``MAX_COVER_PIXELS`` inclusive ``(lo, hi)``
        order-16 NESTED pixel ranges.
    """
    depth = cone_cover_depth(ra_deg, dec_deg, radius_arcsec)
    shift = 2 * (HEALPIX_ORDER - depth)
    return _merge_ranges(
        (int(pixel) << shift, ((int(pixel) + 1) << shift) - 1)
        for pixel in _cone_pixels(ra_deg, dec_deg, radius_arcsec, depth)
    )


def _merge_ranges(pairs) -> list[tuple[int, int]]:
    """Merge sorted, non-overlapping inclusive ``(lo, hi)`` ranges that touch."""
    ranges: list[tuple[int, int]] = []
    for lo, hi in pairs:
        if ranges and lo == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], hi)
        else:
            ranges.append((lo, hi))
    return ranges


def _merge_contiguous(pixels) -> list[tuple[int, int]]:
    """Merge a sorted iterable of pixel indices into contiguous ``[lo, hi]`` ranges."""
    return _merge_ranges((int(value), int(value)) for value in pixels)


def angular_separation_arcsec(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float
) -> float:
    """Return the great-circle separation between two sky positions, in arcsec.

    Uses the haversine formula, which stays numerically stable at the small
    separations a cone fine-filter cares about. This is the exact filter applied
    after the :func:`cone_ipix_ranges` pre-filter narrows candidates by pixel.

    Args:
        ra1_deg: First position right ascension in degrees.
        dec1_deg: First position declination in degrees.
        ra2_deg: Second position right ascension in degrees.
        dec2_deg: Second position declination in degrees.

    Returns:
        Angular separation in arcseconds.
    """
    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    d_dec = dec2 - dec1
    d_ra = ra2 - ra1
    hav = (
        math.sin(d_dec / 2.0) ** 2
        + math.cos(dec1) * math.cos(dec2) * math.sin(d_ra / 2.0) ** 2
    )
    ang_rad = 2.0 * math.asin(min(1.0, math.sqrt(hav)))
    return math.degrees(ang_rad) * 3600.0
