"""In-process positional association of an alert against the TNS snapshot.

A ~160k-row snapshot (``core.models.TnsObject``) is small enough to associate
in process, reusing the read-model's HEALPix toolkit (``core.healpix``) — a
cone pre-filter on the indexed ``healpix_ipix`` column followed by an exact
arcsecond fine-filter — so no Dask, no new dependency, and no version-skew
boundary (plan U6 / KTD1).

Two layers so the caller can choose its access pattern (plan U7 defers the
load-once-vs-per-alert-query tuning): :func:`nearest_within` is a pure function
over any iterable of candidate objects, and :func:`find_tns_match` wraps it with
an ORM cone query.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from django.db.models import Q

from core.healpix import angular_separation_arcsec, cone_ipix_ranges
from core.models import TnsObject


@dataclass(frozen=True)
class TnsMatch:
    """The nearest TNS object to an alert, within the association radius."""

    objid: int
    name: str
    name_prefix: str | None
    type: str | None
    redshift: float | None
    separation_arcsec: float


def _coords_valid(ra_deg: float, dec_deg: float) -> bool:
    """True when the position is safe to associate (finite, dec in range)."""
    return (
        math.isfinite(ra_deg)
        and math.isfinite(dec_deg)
        and -90.0 <= dec_deg <= 90.0
    )


def nearest_within(
    ra_deg: float,
    dec_deg: float,
    radius_arcsec: float,
    candidates: Iterable[TnsObject],
) -> TnsMatch | None:
    """Return the nearest candidate within ``radius_arcsec``, or ``None``.

    Pure and DB-free: ``candidates`` is any iterable of objects exposing
    ``ra_deg``/``dec_deg`` plus the TNS fields, so the caller may pass an ORM
    queryset or an in-memory list loaded once per batch.

    Args:
        ra_deg: Alert right ascension in degrees.
        dec_deg: Alert declination in degrees.
        radius_arcsec: Association radius (``settings.TNS_MATCH_RADIUS_ARCSEC``).
        candidates: Candidate TNS objects (already cone-prefiltered, or all).

    Returns:
        A :class:`TnsMatch` for the nearest object within the radius, or ``None``
        when the position is invalid or no candidate is within range.
    """
    if not _coords_valid(ra_deg, dec_deg):
        return None
    best: TnsMatch | None = None
    best_sep = radius_arcsec
    for obj in candidates:
        sep = angular_separation_arcsec(ra_deg, dec_deg, obj.ra_deg, obj.dec_deg)
        if sep <= best_sep:
            best_sep = sep
            best = TnsMatch(
                objid=obj.objid,
                name=obj.name,
                name_prefix=obj.name_prefix,
                type=obj.type,
                redshift=obj.redshift,
                separation_arcsec=sep,
            )
    return best


def cone_candidates(ra_deg: float, dec_deg: float, radius_arcsec: float):
    """Return a ``TnsObject`` queryset cone-prefiltered on ``healpix_ipix``.

    Uses :func:`core.healpix.cone_ipix_ranges` to build an inclusive
    ``healpix_ipix BETWEEN`` predicate — the same index-backed pattern as the
    read-model cone search. The cover is inclusive, so callers still apply the
    exact :func:`nearest_within` fine-filter.
    """
    if not _coords_valid(ra_deg, dec_deg):
        return TnsObject.objects.none()
    ranges = cone_ipix_ranges(ra_deg, dec_deg, radius_arcsec)
    if not ranges:
        return TnsObject.objects.none()
    predicate = Q()
    for lo, hi in ranges:
        predicate |= Q(healpix_ipix__gte=lo, healpix_ipix__lte=hi)
    return TnsObject.objects.filter(predicate)


def find_tns_match(
    ra_deg: float, dec_deg: float, radius_arcsec: float
) -> TnsMatch | None:
    """Cone-query the snapshot and return the nearest TNS object within the radius.

    A DB-backed convenience over :func:`cone_candidates` + :func:`nearest_within`,
    for callers that query per alert rather than loading the snapshot once.
    """
    if not _coords_valid(ra_deg, dec_deg):
        return None
    return nearest_within(
        ra_deg, dec_deg, radius_arcsec, cone_candidates(ra_deg, dec_deg, radius_arcsec)
    )
