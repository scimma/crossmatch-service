"""Select, write, and load the replay's portable alert sample.

The export runs on PROD against its database, read-only, and selects alerts per
coverage category so a replay exercises every path worth comparing: matches in
each configured catalog, terminal alerts with no match, alerts outside the DES
footprint, and matches whose stored catalog values include nulls.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.db.models import BooleanField, CharField, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import MD5, Cast, Concat
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
def read_only_transaction() -> Iterator[None]:
    """Run the enclosed queries in a transaction Postgres enforces as read-only."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        yield


def _category_querysets() -> dict:
    """Return ``{category: Alert queryset}`` for every coverage category."""
    terminal = Alert.objects.filter(status__in=_TERMINAL_STATUSES)
    categories = {}
    for catalog in settings.CROSSMATCH_CATALOGS:
        name = catalog["name"]
        matched_ids = CatalogMatch.objects.filter(catalog_name=name).values("alert_id")
        categories[f"matched:{name}"] = Alert.objects.filter(
            lsst_diaObject_diaObjectId__in=matched_ids
        )
    categories["no_match"] = terminal.exclude(
        lsst_diaObject_diaObjectId__in=CatalogMatch.objects.values("alert_id")
    )
    categories["outside_des"] = terminal.filter(dec_deg__gt=OUTSIDE_DES_DEC_LIMIT)
    null_ids = (
        CatalogMatch.objects.annotate(
            has_null=RawSQL(
                "jsonb_path_exists(catalog_payload, '$.* ? (@ == null)')",
                [],
                output_field=BooleanField(),
            )
        )
        .filter(has_null=True)
        .values("alert_id")
    )
    categories["null_catalog_values"] = Alert.objects.filter(
        lsst_diaObject_diaObjectId__in=null_ids
    )
    return categories


def select_sample(per_category: int, seed: str) -> dict:
    """Select a coverage sample of historical alerts.

    Within each category alerts are ordered by a hash of diaObjectId and the seed,
    so the same seed over the same data reproduces the sample. Alerts in several
    categories appear once, tagged with each.

    Args:
        per_category: Maximum alerts taken per category.
        seed: Seed for the deterministic pseudo-random order.

    Returns:
        The sample as a JSON-ready dict.
    """
    selected: dict[int, dict] = {}
    counts = {}
    for name, queryset in _category_querysets().items():
        rows = (
            queryset.exclude(ra_deg=math.nan)
            .exclude(dec_deg=math.nan)
            .annotate(
                _order=MD5(
                    Concat(
                        Cast("lsst_diaObject_diaObjectId", CharField()),
                        Value(seed),
                        output_field=CharField(),
                    )
                )
            )
            .order_by("_order", "lsst_diaObject_diaObjectId")
            .values_list("uuid", "lsst_diaObject_diaObjectId", "ra_deg", "dec_deg")[
                :per_category
            ]
        )
        counts[name] = 0
        for uuid, dia_id, ra, dec in rows:
            counts[name] += 1
            entry = selected.setdefault(
                int(dia_id),
                {
                    "uuid": str(uuid),
                    "dia_object_id": int(dia_id),
                    "ra_deg": float(ra),
                    "dec_deg": float(dec),
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
