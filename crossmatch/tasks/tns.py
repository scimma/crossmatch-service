"""Refresh the local TNS snapshot from TNS's public bulk exports (plan U4).

Seeds ``core.models.TnsObject`` from TNS's daily full file when the snapshot is
empty or stale, and merges the current hour's delta otherwise, upserting by
``objid`` and stamping ``TnsSnapshotMeta`` with the refresh epoch. The upsert
and the epoch write share a transaction so a mid-refresh failure never leaves a
partially-loaded snapshot marked current.

Fail-soft by construction: any download/auth/parse failure logs and returns,
leaving the prior snapshot intact — it never raises into Celery Beat and never
touches the crossmatch batch. Invoked by the ``refresh_tns_snapshot`` task in
``tasks/schedule.py``.
"""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core import tns as tns_client
from core.healpix import radec_to_ipix_array
from core.log import get_logger
from core.models import TnsObject, TnsSnapshotMeta

logger = get_logger(__name__)

_UPSERT_FIELDS = [
    "name",
    "name_prefix",
    "ra_deg",
    "dec_deg",
    "type",
    "redshift",
    "healpix_ipix",
    "updated_at",
]


def _credentials_configured() -> bool:
    """True when all three TNS bot credentials are set."""
    return bool(
        settings.TNS_BOT_ID and settings.TNS_BOT_NAME and settings.TNS_BOT_API_KEY
    )


def _snapshot_is_stale(now: datetime) -> bool:
    """True when there is no snapshot or its epoch is older than the max age."""
    meta = TnsSnapshotMeta.objects.first()
    if meta is None or meta.last_refresh_epoch is None:
        return True
    age_seconds = (now - meta.last_refresh_epoch).total_seconds()
    return age_seconds > settings.TNS_SNAPSHOT_MAX_AGE_SECONDS


def _upsert_records(records, epoch: datetime) -> int:
    """Upsert object records by ``objid`` and stamp the snapshot epoch, atomically.

    Args:
        records: Parsed :class:`core.tns.TnsObjectRecord` instances.
        epoch: The refresh epoch to record on success.

    Returns:
        The number of records upserted.
    """
    if not records:
        # Nothing to write, but still advance the epoch so currency reflects the
        # successful (empty) delta fetch.
        with transaction.atomic():
            meta, _ = TnsSnapshotMeta.objects.get_or_create(pk=1)
            meta.last_refresh_epoch = epoch
            meta.save(update_fields=["last_refresh_epoch", "updated_at"])
        return 0

    ipix = radec_to_ipix_array(
        [r.ra_deg for r in records], [r.dec_deg for r in records]
    )
    rows = [
        TnsObject(
            objid=r.objid,
            name=r.name,
            name_prefix=r.name_prefix,
            ra_deg=r.ra_deg,
            dec_deg=r.dec_deg,
            type=r.type,
            redshift=r.redshift,
            healpix_ipix=pixel,
        )
        for r, pixel in zip(records, ipix)
    ]
    with transaction.atomic():
        TnsObject.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["objid"],
            update_fields=_UPSERT_FIELDS,
            batch_size=5000,
        )
        meta, _ = TnsSnapshotMeta.objects.get_or_create(pk=1)
        meta.last_refresh_epoch = epoch
        meta.save(update_fields=["last_refresh_epoch", "updated_at"])
    return len(rows)


def refresh_snapshot(now: datetime | None = None) -> dict:
    """Refresh the TNS snapshot, fail-soft.

    Args:
        now: Override for the current time (tests); defaults to ``timezone.now()``.

    Returns:
        A small status dict (``status`` is ``ok`` / ``skipped`` / ``failed``) for
        logging and tests. Never raises for a download/parse failure.
    """
    now = now or timezone.now()
    if not _credentials_configured():
        logger.warning("tns_refresh_skipped_no_credentials")
        return {"status": "skipped", "reason": "no_credentials"}

    seed = _snapshot_is_stale(now)
    filename = (
        tns_client.FULL_OBJECTS_FILENAME
        if seed
        else tns_client.hourly_delta_filename(now.hour)
    )
    try:
        records = tns_client.fetch_object_records(
            base_url=settings.TNS_OBJECTS_BASE_URL,
            filename=filename,
            bot_id=settings.TNS_BOT_ID,
            bot_name=settings.TNS_BOT_NAME,
            api_key=settings.TNS_BOT_API_KEY,
        )
    except tns_client.TnsClientError as exc:
        # Fail-soft: keep the prior snapshot; do not raise into Beat.
        logger.error("tns_refresh_failed", seed=seed, filename=filename, error=str(exc))
        return {"status": "failed", "reason": str(exc)}

    upserted = _upsert_records(records, now)
    logger.info(
        "tns_refresh_complete", seed=seed, filename=filename, upserted=upserted
    )
    return {"status": "ok", "seed": seed, "upserted": upserted}
