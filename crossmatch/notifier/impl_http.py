"""HTTP implementation of the LSST return notification — deferred to future work."""


def post_notification(endpoint_url: str, payload: dict, timeout: float = 30.0) -> int:
    """POST payload to endpoint_url; returns HTTP status code."""
    raise NotImplementedError("deferred to future work")
