"""Gaia DR3 crossmatch via LSDB — deferred to future work."""


def crossmatch_alert_against_gaia(
    ra_deg: float,
    dec_deg: float,
    radius_arcsec: float,
    match_version: int = 1,
) -> list[dict]:
    """Return list of Gaia DR3 matches within radius_arcsec of (ra_deg, dec_deg)."""
    raise NotImplementedError("deferred to future work")
