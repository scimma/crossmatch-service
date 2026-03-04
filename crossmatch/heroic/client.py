"""HEROIC API client — deferred to future work."""


class HEROICClient:
    """HTTP client for the HEROIC planned-pointings API."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url
        self.timeout = timeout

    def get_planned_pointings(self) -> list[dict]:
        raise NotImplementedError("deferred to future work")
