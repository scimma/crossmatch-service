"""Replay a sample through the shared crossmatch compute step into a snapshot.

A snapshot holds every match the replay computed -- keyed by diaObjectId and
catalog, with the exact payload production would publish -- plus the run
context needed to judge whether two snapshots are comparable: versions on the
client and the Dask workers, image tag, crossmatch configuration, per-catalog
outcomes and identity, and the TNS snapshot's state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core.dask import check_cluster_alignment, cluster_package_versions
from core.healpix import angular_separation_arcsec
from core.log import get_logger
from core.models import TnsSnapshotMeta
from matching.tns_match import cone_candidates
from replay.sample import LoadedSample
from tasks.crossmatch import compute_crossmatch

logger = get_logger(__name__)

SNAPSHOT_KIND = "crossmatch-replay-snapshot"
SNAPSHOT_FORMAT_VERSION = 1

# hats catalog properties that identify a published catalog build, when present.
_CATALOG_IDENTITY_KEYS = ("hats_creation_date", "hats_version", "hats_release_date")

__all__ = [
    "SNAPSHOT_FORMAT_VERSION",
    "SNAPSHOT_KIND",
    "build_snapshot",
    "catalog_identity",
    "check_cluster_alignment",
    "cluster_package_versions",
    "tns_fingerprint",
    "write_snapshot",
]


def _sha256(value) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def tns_fingerprint(ra_deg: float, dec_deg: float, radius_arcsec: float) -> str:
    """Identify the TNS content an alert's association reads.

    Hashes the published-relevant fields of every TNS object within the match
    radius of the alert, so the fingerprint changes only when those objects do --
    not when the hourly refresh advances the epoch or adds objects elsewhere.

    Args:
        ra_deg: Alert right ascension.
        dec_deg: Alert declination.
        radius_arcsec: The TNS match radius.

    Returns:
        A hex digest.
    """
    objects = sorted(
        (obj.objid, obj.name, obj.type, obj.redshift, obj.ra_deg, obj.dec_deg)
        for obj in cone_candidates(ra_deg, dec_deg, radius_arcsec)
        if angular_separation_arcsec(ra_deg, dec_deg, obj.ra_deg, obj.dec_deg)
        <= radius_arcsec
    )
    return _sha256(objects)


def catalog_identity(catalog_config: dict) -> dict:
    """Best-effort identity of a hosted HATS catalog build.

    Records the URL plus the catalog's row count and build properties when LSDB
    exposes them, so a catalog republished at the same URL shows up as a
    run-context difference rather than as a stack change.

    Args:
        catalog_config: The catalog's ``CROSSMATCH_CATALOGS`` entry.

    Returns:
        ``{'hats_url', ...properties}``, or with ``error`` when unreadable.
    """
    from matching.catalog import _get_catalog

    identity = {"hats_url": catalog_config["hats_url"]}
    try:
        info = _get_catalog(catalog_config).hc_structure.catalog_info
        identity["total_rows"] = info.total_rows
        extra = info.model_extra or {}
        for key in _CATALOG_IDENTITY_KEYS:
            if key in extra:
                identity[key] = str(extra[key])
    except Exception as exc:
        identity["error"] = str(exc)
    return identity


def _crossmatch_config() -> dict:
    return {
        "radius_arcsec": settings.CROSSMATCH_RADIUS_ARCSEC,
        "catalogs": [
            {
                "name": cfg["name"],
                "hats_url": cfg["hats_url"],
                "source_id_column": cfg["source_id_column"],
                "ra_column": cfg["ra_column"],
                "dec_column": cfg["dec_column"],
                "payload_columns": list(cfg.get("payload_columns", [])),
            }
            for cfg in settings.CROSSMATCH_CATALOGS
        ],
        "tns_match_radius_arcsec": settings.TNS_MATCH_RADIUS_ARCSEC,
        "tns_snapshot_max_age_seconds": settings.TNS_SNAPSHOT_MAX_AGE_SECONDS,
        "tns_object_url_template": settings.TNS_OBJECT_URL_TEMPLATE,
    }


def build_snapshot(sample: LoadedSample, image_tag: str, versions: dict) -> dict:
    """Replay a sample through the shared compute step and build its snapshot.

    Writes nothing to the database and publishes nothing: the compute step runs
    without the persistence callbacks production passes.

    Args:
        sample: The loaded sample.
        image_tag: The deployed image tag the replay ran on.
        versions: Client/scheduler/worker package versions.

    Returns:
        The snapshot as a JSON-ready dict.
    """
    started = timezone.now()
    result = compute_crossmatch(sample.rows, now=started)

    matches = [
        {
            "dia_object_id": int(record.dia_object_id),
            "catalog": record.catalog_name,
            "source_id": record.source_id,
            "dist_arcsec": float(record.dist_arcsec),
            "source_ra_deg": float(record.source_ra_deg),
            "source_dec_deg": float(record.source_dec_deg),
            "catalog_payload": record.catalog_payload,
            "published_payload": record.published_payload,
        }
        for record in result.records
    ]
    matches.sort(key=lambda m: (m["dia_object_id"], m["catalog"], m["source_id"]))

    radius = settings.TNS_MATCH_RADIUS_ARCSEC
    fingerprints = {
        str(dia_id): tns_fingerprint(ra, dec, radius)
        for _, dia_id, ra, dec in sample.rows
    }
    meta = TnsSnapshotMeta.objects.first()
    tns = result.tns
    return {
        "kind": SNAPSHOT_KIND,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "run_context": {
            "started_at": started.isoformat(),
            "finished_at": timezone.now().isoformat(),
            "image_tag": image_tag,
            "versions": versions,
            "sample": {
                "digest": sample.digest,
                "seed": sample.data.get("seed"),
                "exported_at": sample.data.get("exported_at"),
                "alert_count": len(sample.rows),
            },
            "crossmatch": _crossmatch_config(),
            "catalog_outcomes": result.catalog_outcomes,
            "catalog_identity": {
                cfg["name"]: catalog_identity(cfg)
                for cfg in settings.CROSSMATCH_CATALOGS
            },
            "tns": {
                "current": bool(tns and tns.current),
                "epoch": (
                    tns.epoch.isoformat() if tns and tns.epoch is not None else None
                ),
                "last_refresh_epoch": (
                    meta.last_refresh_epoch.isoformat()
                    if meta is not None and meta.last_refresh_epoch is not None
                    else None
                ),
                "content_identity": _sha256(sorted(fingerprints.items())),
                "fingerprints": fingerprints,
            },
        },
        "matches": matches,
    }


def write_snapshot(snapshot: dict, path: Path | str) -> None:
    """Write a snapshot as JSON; NaN is rejected so the file stays valid JSON."""
    Path(path).write_text(json.dumps(snapshot, indent=1, allow_nan=False) + "\n")
