"""LSST return-channel notification protocol — deferred to future work."""
from core.models import GaiaMatch, Notification


def send_match_notification(match: GaiaMatch) -> Notification:
    """Send a notification back to LSST for a given Gaia match."""
    raise NotImplementedError("deferred to future work")
