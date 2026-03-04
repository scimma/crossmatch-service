"""Refresh planned_pointings from HEROIC — deferred to future work."""
from heroic.client import HEROICClient


def refresh_planned_pointings(heroic_url: str) -> None:
    """Fetch planned pointings from HEROIC and replace the DB table."""
    raise NotImplementedError("deferred to future work")
