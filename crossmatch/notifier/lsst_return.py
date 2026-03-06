"""LSST return-channel notification protocol — deferred to future work."""
from core.models import CatalogMatch, Notification


def send_match_notification(match: CatalogMatch) -> Notification:
    """Send a notification back to LSST for a given catalog match."""
    raise NotImplementedError("deferred to future work")
