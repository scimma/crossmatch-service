"""Planned-pointing footprint filter — deferred to future work."""
from core.models import PlannedPointing


def pointings_covering(ra_deg: float, dec_deg: float, event_mjd: float) -> list[PlannedPointing]:
    """Return planned pointings that cover (ra_deg, dec_deg) within their time window."""
    raise NotImplementedError("deferred to future work")
