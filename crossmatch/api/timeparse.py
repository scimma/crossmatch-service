"""Shared ISO-8601 timestamp parsing for API query parameters.

Used by ``recent-crossmatches`` (``api/views.py``) and the window summary
(``api/summary.py``) so both read ``start``/``end`` by the same rule: ISO-8601,
with a naive value taken as UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware

from api.errors import InvalidQuery


def parse_timestamp(raw: Any, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a naive value as UTC.

    Args:
        raw: The raw parameter value.
        field: The parameter name, for the error message.

    Returns:
        An aware ``datetime``; a naive input is made aware in UTC, an aware
        input keeps its offset.

    Raises:
        InvalidQuery: If the value is not a string holding a valid ISO-8601
            timestamp (``param`` names ``field``).
    """
    try:
        parsed = parse_datetime(raw) if isinstance(raw, str) else None
    except ValueError:
        # Well-formed but impossible, e.g. month 13.
        parsed = None
    if parsed is None:
        raise InvalidQuery(
            f"{field} is not a valid ISO-8601 timestamp: {raw!r}", param=field
        )
    if is_naive(parsed):
        parsed = make_aware(parsed, timezone.utc)
    return parsed
