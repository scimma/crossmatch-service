"""Classify the identifier tool's inputs (KTD6, R9).

The tool takes ``identifiers: array of strings``. Each one is a Rubin
``diaObjectId`` (all decimal digits), a TNS name (anything
``normalize_tns_name`` accepts), or unrecognized. A bare JSON number is
accepted as an ID only while it is exactly representable as a double: above
2^53 a JavaScript client layer may already have rounded it, so it is answered
with a reason asking for the string form instead of being looked up.

Pure functions: no database. Raw input text never reaches the output; only
the canonical forms do (digits, or a normalized TNS designation), which also
keeps the API request a truncated answer carries free of quoting hazards.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from api.contract import parse_dia_object_id
from api.errors import InvalidQuery
from api.positions import normalize_tns_name

KIND_ID = 'id'
KIND_TNS = 'tns'
KIND_UNRECOGNIZED = 'unrecognized'
KIND_PRECISION_LOST = 'precision_lost'

UNRECOGNIZED_REASON = (
    'Not a recognized identifier. Give a Rubin diaObjectId as a string of '
    'decimal digits, or a TNS name such as 2026abc, AT 2026abc or SN 2026abc. '
    'Names from other surveys (for example ZTF names) are not accepted.'
)
PRECISION_LOST_REASON = (
    'This diaObjectId arrived as a JSON number larger than 2^53, so it may '
    'already have been rounded on the way here. Send it again as a string of '
    'decimal digits.'
)

_EXACT_MAX = 2**53


@dataclass(frozen=True)
class Identifier:
    """One classified identifier.

    Attributes:
        index: Position of its first occurrence in the caller's list.
        kind: ``KIND_ID``, ``KIND_TNS``, ``KIND_UNRECOGNIZED`` or
            ``KIND_PRECISION_LOST``.
        value: The canonical form: the decimal ``diaObjectId`` string or the
            normalized TNS designation; ``None`` when not looked up.
        reason: Why it is not looked up; ``None`` when it is.
    """

    index: int
    kind: str
    value: str | None = None
    reason: str | None = None

    def lookup_input(self) -> dict[str, Any] | None:
        """The ``lookup_objects`` input for this identifier.

        Returns:
            ``{'kind': 'id', 'diaObjectId': <str>}``, ``{'kind': 'tns',
            'name': <str>}``, or ``None`` when it is not looked up.
        """
        if self.kind == KIND_ID:
            return {'kind': 'id', 'diaObjectId': self.value}
        if self.kind == KIND_TNS:
            return {'kind': 'tns', 'name': self.value}
        return None


@dataclass(frozen=True)
class Classified:
    """The identifier list after classification and de-duplication.

    Attributes:
        requested: How many identifiers the caller sent, duplicates included.
        identifiers: The distinct identifiers, in first-seen order.
    """

    requested: int
    identifiers: list[Identifier]

    def lookup_inputs(self) -> list[dict[str, Any]]:
        """The ``lookup_objects`` inputs of the looked-up identifiers, in order.

        Returns:
            One tagged input per ``id`` or ``tns`` identifier.
        """
        return [
            entry for entry in (i.lookup_input() for i in self.identifiers)
            if entry is not None
        ]


def _classify_one(value: Any) -> tuple[str, str | None]:
    """Classify one raw identifier; returns ``(kind, canonical value)``."""
    if isinstance(value, bool):
        return KIND_UNRECOGNIZED, None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return KIND_UNRECOGNIZED, None
        if abs(value) > _EXACT_MAX:
            return KIND_PRECISION_LOST, None
        if value < 0:
            return KIND_UNRECOGNIZED, None
        return KIND_ID, str(int(value))
    if not isinstance(value, str):
        return KIND_UNRECOGNIZED, None
    text = value.strip()
    if text.isascii() and text.isdigit():
        try:
            return KIND_ID, str(parse_dia_object_id(text))
        except InvalidQuery:  # past int64, or past Python's int-string limit
            return KIND_UNRECOGNIZED, None
    try:
        return KIND_TNS, normalize_tns_name(text)
    except ValueError:
        return KIND_UNRECOGNIZED, None


def classify_identifiers(values: Sequence[Any]) -> Classified:
    """Classify and de-duplicate the identifier tool's inputs (KTD6).

    An identifier is a duplicate when it has the same kind and canonical form
    as an earlier one (``SN 2026abc`` and ``2026ABC`` are the same name).
    Unrecognized and precision-lost entries have no canonical form, so each
    one is reported at its own position.

    Args:
        values: The ``identifiers`` argument as decoded from JSON.

    Returns:
        The classified identifiers.
    """
    seen: set[tuple[str, str]] = set()
    identifiers: list[Identifier] = []
    for index, value in enumerate(values):
        kind, canonical = _classify_one(value)
        if canonical is not None:
            if (kind, canonical) in seen:
                continue
            seen.add((kind, canonical))
            identifiers.append(Identifier(index=index, kind=kind, value=canonical))
        else:
            reason = (
                PRECISION_LOST_REASON if kind == KIND_PRECISION_LOST
                else UNRECOGNIZED_REASON
            )
            identifiers.append(Identifier(index=index, kind=kind, reason=reason))
    return Classified(requested=len(values), identifiers=identifiers)
