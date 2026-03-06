"""Alert normalization — maps broker-specific alert schemas to internal format.

Deferred to future work.
"""


def normalize_antares(raw_alert: dict) -> dict:
    """Normalize an ANTARES alert to the internal alert format."""
    raise NotImplementedError("deferred to future work")


def normalize_lasair(raw_alert: dict) -> dict:
    """Normalize a Lasair alert to the internal alert format."""
    raise NotImplementedError("deferred to future work")
