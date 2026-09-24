"""Select, write, and load the replay's portable alert sample.

The export runs on PROD against its database, read-only, and selects alerts per
coverage category so a replay exercises every path worth comparing: matches in
each configured catalog, terminal alerts with no match, alerts outside the DES
footprint, and matches whose stored catalog values include nulls.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.db.models import BooleanField, Exists, F, Max, Min, OuterRef
from django.db.models.expressions import RawSQL
from django.utils import timezone

from core.models import Alert, CatalogMatch
from replay.formats import content_digest, write_json

SAMPLE_KIND = "crossmatch-replay-sample"
SAMPLE_FORMAT_VERSION = 1

#: DES Y6 Gold ends near dec +5; alerts north of this are outside its footprint.
OUTSIDE_DES_DEC_LIMIT = 10.0

_TERMINAL_STATUSES = (Alert.Status.MATCHED, Alert.Status.NOTIFIED)


class SampleFormatError(ValueError):
    """A sample file is malformed or of an unsupported format version."""


@dataclass
class LoadedSample:
    """A validated sample file.

    Attributes:
        data: The parsed file.
        rows: ``(uuid, diaObjectId, ra_deg, dec_deg)`` tuples, the shape
            ``crossmatch_batch`` loads alerts in.
        digest: SHA-256 of the canonical alert list, identifying the sample.
    """

    data: dict
    rows: list
    digest: str


@contextmanager
def read_only_transaction(
    statement_timeout_seconds: int | None = None,
) -> Iterator[None]:
    """Run the enclosed queries in a transaction Postgres enforces as read-only.

    Args:
        statement_timeout_seconds: When set, Postgres cancels any single query in
            the transaction that runs longer, so an export can never hold a long
            query against the PROD database.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            if statement_timeout_seconds:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [f"{int(statement_timeout_seconds)}s"],
                )
        yield


def _seed_start(seed: str) -> int | None:
    """Map a seed to a diaObjectId inside the current id range.

    Every category walks its index upward from this point, so the same seed over
    the same data selects the same alerts. Only the indexed min and max are read.

    Args:
        seed: The sample seed.

    Returns:
        A diaObjectId between the smallest and largest present, or ``None`` when
        there are no alerts.
    """
    bounds = Alert.objects.aggregate(
        lo=Min("lsst_diaObject_diaObjectId"), hi=Max("lsst_diaObject_diaObjectId")
    )
    lo, hi = bounds["lo"], bounds["hi"]
    if lo is None:
        return None
    offset = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big")
    return lo + offset % (hi - lo + 1)


def _category_id_querysets() -> dict:
    """Return ``{category: queryset of diaObjectIds}`` for every coverage category.

    Each queryset yields a single ``dia_id`` column that is backed by an index
    (the alert's unique diaObjectId, or the match table's alert index), so a walk
    ordered by it with a limit stops after reading about as many rows as it keeps
    rather than hashing and sorting every qualifying row.
    """
    terminal = Alert.objects.filter(status__in=_TERMINAL_STATUSES)
    alert_ids = F("lsst_diaObject_diaObjectId")
    categories = {}
    for catalog in settings.CROSSMATCH_CATALOGS:
        categories[f"matched:{catalog['name']}"] = CatalogMatch.objects.filter(
            catalog_name=catalog["name"]
        ).annotate(dia_id=F("alert_id"))
    categories["no_match"] = terminal.filter(
        ~Exists(
            CatalogMatch.objects.filter(alert_id=OuterRef("lsst_diaObject_diaObjectId"))
        )
    ).annotate(dia_id=alert_ids)
    categories["outside_des"] = terminal.filter(
        dec_deg__gt=OUTSIDE_DES_DEC_LIMIT
    ).annotate(dia_id=alert_ids)
    categories["null_catalog_values"] = (
        CatalogMatch.objects.annotate(
            has_null=RawSQL(
                "jsonb_path_exists(catalog_payload, '$.* ? (@ == null)')",
                [],
                output_field=BooleanField(),
            )
        )
        .filter(has_null=True)
        .annotate(dia_id=F("alert_id"))
    )
    return categories


def _walk(queryset, start: int, limit: int) -> list:
    """Take up to ``limit`` distinct ids at or after ``start``, wrapping around.

    Args:
        queryset: A category queryset carrying an indexed ``dia_id`` column.
        start: The seed-derived starting diaObjectId.
        limit: How many ids to take.

    Returns:
        The selected diaObjectIds, in walk order.
    """
    ids: list = []
    for part in (queryset.filter(dia_id__gte=start), queryset.filter(dia_id__lt=start)):
        if len(ids) >= limit:
            break
        # A match table can hold one row per match version; skip repeats here
        # rather than with DISTINCT, which would force a sort of the whole walk.
        rows = part.order_by("dia_id").values_list("dia_id", flat=True)
        for dia_id in rows.iterator(chunk_size=limit * 4):
            if dia_id not in ids:
                ids.append(dia_id)
            if len(ids) >= limit:
                break
    return ids


def select_sample(per_category: int, seed: str) -> dict:
    """Select a coverage sample of historical alerts.

    Each category walks its index upward from a seed-derived diaObjectId (wrapping
    to the start of the range when needed), so the same seed over the same data
    reproduces the sample and the cost does not grow with the size of the tables.
    Alerts in several categories appear once, tagged with each; alerts without
    finite coordinates are skipped.

    Args:
        per_category: Maximum alerts taken per category.
        seed: Seed for the deterministic starting point.

    Returns:
        The sample as a JSON-ready dict.
    """
    start = _seed_start(seed)
    category_ids = {}
    for name, queryset in _category_id_querysets().items():
        category_ids[name] = (
            [] if start is None else _walk(queryset, start, per_category)
        )

    wanted = {dia_id for ids in category_ids.values() for dia_id in ids}
    rows = {
        int(dia_id): (uuid, float(ra), float(dec))
        for uuid, dia_id, ra, dec in Alert.objects.filter(
            lsst_diaObject_diaObjectId__in=wanted
        ).values_list("uuid", "lsst_diaObject_diaObjectId", "ra_deg", "dec_deg")
        if math.isfinite(ra) and math.isfinite(dec)
    }

    selected: dict[int, dict] = {}
    counts = {}
    for name, ids in category_ids.items():
        counts[name] = 0
        for dia_id in ids:
            if int(dia_id) not in rows:
                continue
            uuid, ra, dec = rows[int(dia_id)]
            counts[name] += 1
            entry = selected.setdefault(
                int(dia_id),
                {
                    "uuid": str(uuid),
                    "dia_object_id": int(dia_id),
                    "ra_deg": ra,
                    "dec_deg": dec,
                    "categories": [],
                },
            )
            entry["categories"].append(name)

    return {
        "kind": SAMPLE_KIND,
        "format_version": SAMPLE_FORMAT_VERSION,
        "exported_at": timezone.now().isoformat(),
        "seed": seed,
        "per_category": per_category,
        "outside_des_dec_limit": OUTSIDE_DES_DEC_LIMIT,
        "categories": counts,
        "unfilled_categories": sorted(name for name, n in counts.items() if n == 0),
        "alerts": [selected[key] for key in sorted(selected)],
    }


def write_sample(sample: dict, path: Path | str) -> None:
    """Write a sample as JSON; NaN is rejected so the file stays valid JSON."""
    write_json(sample, path)


def alerts_digest(alerts: list) -> str:
    """SHA-256 of the canonical alert list, identifying a sample's input."""
    return content_digest(alerts)


def load_sample(path: Path | str) -> LoadedSample:
    """Load and validate a sample file.

    Args:
        path: The sample file.

    Returns:
        The :class:`LoadedSample`.

    Raises:
        SampleFormatError: Wrong kind or format version, or a malformed alert --
            including a diaObjectId that is not an exact integer.
    """
    data = json.loads(Path(path).read_text())
    if data.get("kind") != SAMPLE_KIND:
        raise SampleFormatError(f'not a replay sample (kind={data.get("kind")!r})')
    if data.get("format_version") != SAMPLE_FORMAT_VERSION:
        raise SampleFormatError(
            f'unsupported format_version {data.get("format_version")!r}; '
            f"expected {SAMPLE_FORMAT_VERSION}"
        )
    rows = []
    for alert in data.get("alerts", []):
        dia_id = alert.get("dia_object_id")
        # diaObjectId is a 64-bit integer and must never pass through a float.
        if not isinstance(dia_id, int) or isinstance(dia_id, bool):
            raise SampleFormatError(f"dia_object_id must be an integer, got {dia_id!r}")
        try:
            rows.append(
                (
                    UUID(alert["uuid"]),
                    dia_id,
                    float(alert["ra_deg"]),
                    float(alert["dec_deg"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SampleFormatError(f"malformed alert {alert!r}: {exc}") from exc
    return LoadedSample(
        data=data, rows=rows, digest=alerts_digest(data.get("alerts", []))
    )
