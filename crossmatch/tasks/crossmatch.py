from dataclasses import dataclass, field
from datetime import datetime

import lsdb
import pandas as pd
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from core.models import Alert, CatalogMatch, Notification, TnsAssociation, TnsSnapshotMeta
from matching.catalog import crossmatch_alerts, is_transient_read_error
from matching.payload import build_catalog_payload, build_published_payload
from matching.tns_match import find_tns_match, tns_payload
from core.log import get_logger
from core.metrics import CATALOG_SKIPS, CROSSMATCH_BATCHES, CROSSMATCH_MATCHES
logger = get_logger(__name__)


# Per-catalog crossmatch outcomes recorded by compute_crossmatch. A catalog
# "succeeds" when its read completes -- matched, empty, or no-overlap -- and only a
# read error whose retries are exhausted is a skip.
CATALOG_MATCHED = 'matched'
CATALOG_EMPTY = 'empty'
CATALOG_NO_OVERLAP = 'no_overlap'
CATALOG_SKIPPED = 'skipped'


@dataclass
class TnsResult:
    """TNS enrichment computed for a batch, before anything is persisted.

    Attributes:
        current: Whether a current TNS snapshot was available for matching.
        epoch: The snapshot epoch used (``None`` when not current).
        enrichment: ``{diaObjectId: {'tns', 'tns_checked', 'tns_snapshot_epoch'}}``
            for the payload builder.
        associations: ``TnsAssociation`` field dicts for the persist step.
    """

    current: bool = False
    epoch: datetime | None = None
    enrichment: dict = field(default_factory=dict)
    associations: list = field(default_factory=list)


@dataclass
class MatchRecord:
    """One catalog match with the payloads production would store and publish."""

    dia_object_id: int
    catalog_name: str
    source_id: str
    dist_arcsec: float
    source_ra_deg: float
    source_dec_deg: float
    catalog_payload: dict
    published_payload: dict


@dataclass
class CrossmatchResult:
    """The in-memory result of crossmatching a set of alerts.

    Attributes:
        alert_count: Alert rows supplied.
        crossmatched_count: Rows with valid coordinates that were crossmatched.
        records: Every buildable match, in catalog then row order.
        catalog_outcomes: ``{catalog_name: CATALOG_*}`` in configured order.
        tns: The TNS enrichment state, or ``None`` when nothing was crossmatched.
    """

    alert_count: int = 0
    crossmatched_count: int = 0
    records: list = field(default_factory=list)
    catalog_outcomes: dict = field(default_factory=dict)
    tns: TnsResult | None = None

    @property
    def skipped_catalogs(self) -> list:
        """Sorted names of catalogs skipped after a transient read failure."""
        return sorted(
            name for name, outcome in self.catalog_outcomes.items()
            if outcome == CATALOG_SKIPPED
        )


def _compute_tns_enrichment(clean_df, now=None) -> TnsResult:
    """Compute per-alert TNS associations for a batch without persisting them.

    For each alert (keyed by ``diaObjectId``) find the nearest TNS object within
    ``TNS_MATCH_RADIUS_ARCSEC`` when a *current* snapshot exists, and build both the
    ``TnsAssociation`` fields (so the API ``full`` level can reconstruct the block)
    and the per-alert enrichment for the payload builder.

    Fail-soft (R8): a missing/stale snapshot or any error yields no ``tns`` block
    for the affected alert(s) and never aborts the batch -- the batch transitions
    to MATCHED unconditionally, so a raised error would permanently lose it.
    ``SoftTimeLimitExceeded`` is always re-raised so the batch self-reverts.

    Note: this queries the snapshot once per alert via the healpix cone index;
    loading the snapshot once and matching in-memory is the plan's deferred tuning.

    Args:
        clean_df: The batch alert DataFrame (``lsst_diaObject_diaObjectId``,
            ``ra_deg``, ``dec_deg``, valid coordinates only).
        now: Override for the current time (tests).

    Returns:
        The computed :class:`TnsResult`.
    """
    now = now or timezone.now()
    try:
        meta = TnsSnapshotMeta.objects.first()
        current = (
            meta is not None
            and meta.last_refresh_epoch is not None
            and (now - meta.last_refresh_epoch).total_seconds()
            <= settings.TNS_SNAPSHOT_MAX_AGE_SECONDS
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.exception('TNS snapshot currency check failed; skipping TNS enrichment')
        return TnsResult()
    epoch = meta.last_refresh_epoch if current else None

    result = TnsResult(current=current, epoch=epoch)
    for tns_row in clean_df.itertuples(index=False):
        # The entire per-alert body is guarded so one bad row (a non-coercible
        # id, a misconfigured url template) yields no tns block for that alert
        # rather than raising into the batch, which would revert it permanently
        # (R8). SoftTimeLimitExceeded is still re-raised for the batch self-revert.
        try:
            dia_id = int(tns_row.lsst_diaObject_diaObjectId)
            match = (
                find_tns_match(
                    tns_row.ra_deg, tns_row.dec_deg, settings.TNS_MATCH_RADIUS_ARCSEC
                )
                if current
                else None
            )
            if not current:
                result.associations.append({'alert_id': dia_id, 'checked': False})
                result.enrichment[dia_id] = {'tns': None, 'tns_checked': False,
                                             'tns_snapshot_epoch': None}
            elif match is None:
                result.associations.append(
                    {'alert_id': dia_id, 'checked': True, 'snapshot_epoch': epoch}
                )
                result.enrichment[dia_id] = {'tns': None, 'tns_checked': True,
                                             'tns_snapshot_epoch': epoch}
            else:
                result.associations.append({
                    'alert_id': dia_id, 'checked': True, 'snapshot_epoch': epoch,
                    'objid': match.objid, 'name': match.name,
                    'name_prefix': match.name_prefix, 'type': match.type,
                    'redshift': match.redshift,
                    'separation_arcsec': match.separation_arcsec,
                })
                result.enrichment[dia_id] = {
                    'tns': tns_payload(
                        objid=match.objid, name=match.name, type=match.type,
                        redshift=match.redshift,
                        separation_arcsec=match.separation_arcsec,
                        url_template=settings.TNS_OBJECT_URL_TEMPLATE,
                    ),
                    'tns_checked': True,
                    'tns_snapshot_epoch': epoch,
                }
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            logger.exception('TNS association failed for alert; no tns block')
            continue
    return result


def _persist_tns_associations(tns: TnsResult) -> None:
    """Upsert the computed TNS associations; a failure is logged, never raised.

    Args:
        tns: The enrichment computed by :func:`_compute_tns_enrichment`.
    """
    try:
        if tns.associations:
            TnsAssociation.objects.bulk_create(
                [TnsAssociation(**fields) for fields in tns.associations],
                update_conflicts=True,
                unique_fields=['alert'],
                update_fields=['checked', 'snapshot_epoch', 'objid', 'name',
                               'name_prefix', 'type', 'redshift',
                               'separation_arcsec', 'updated_at'],
                batch_size=5000,
            )
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        # Non-fatal: the Hopskotch payload still carries the computed block; only
        # the API full-level reconstruction lacks the persisted rows this batch.
        logger.exception('Persisting TNS associations failed; publishing without persist')


def _build_tns_associations(clean_df, now=None):
    """Compute and persist per-alert TNS associations for a batch (plan U7).

    Args:
        clean_df: The batch alert DataFrame (valid coordinates only).
        now: Override for the current time (tests).

    Returns:
        ``{diaObjectId: {'tns': dict|None, 'tns_checked': bool,
        'tns_snapshot_epoch': datetime|None}}`` for the payload build loop.
    """
    tns = _compute_tns_enrichment(clean_df, now=now)
    _persist_tns_associations(tns)
    return tns.enrichment


def _catalog_records(result_df, catalog_config, tns_enrichment) -> list:
    """Build the match records for one catalog's crossmatch result.

    Each row is built defensively: an unexpected value in one row logs and skips
    that row without dropping the rest of the catalog's matches or aborting the
    batch (R8).

    Args:
        result_df: The catalog's non-empty crossmatch result.
        catalog_config: The catalog's ``CROSSMATCH_CATALOGS`` entry.
        tns_enrichment: Per-alert TNS enrichment keyed by diaObjectId.

    Returns:
        The catalog's :class:`MatchRecord` list.
    """
    catalog_name = catalog_config['name']
    source_id_col = catalog_config['source_id_column']
    ra_col = catalog_config['ra_column']
    dec_col = catalog_config['dec_column']
    payload_cols = catalog_config.get('payload_columns', [])

    # Rename _dist_arcsec so itertuples() can access it
    # (namedtuple fields cannot start with underscore)
    result_df = result_df.rename(columns={'_dist_arcsec': 'dist_arcsec'})

    records = []
    for row in result_df.itertuples(index=False):
        try:
            dia_id = int(row.lsst_diaObject_diaObjectId)
            src_id = str(getattr(row, source_id_col))
            dist = row.dist_arcsec
            ra = getattr(row, ra_col)
            dec = getattr(row, dec_col)

            # Catalog-specific core columns: lowercase keys, JSON-native
            # values, stable key set (see matching/payload.py). Stored on
            # the match record and nested under 'catalog_payload' in the
            # published notification; top-level metadata is unchanged.
            catalog_payload = build_catalog_payload(
                {col: getattr(row, col) for col in payload_cols},
                payload_cols,
            )
            tns_info = tns_enrichment.get(dia_id, {})
            published_payload = build_published_payload(
                dia_id, ra, dec, catalog_name, src_id, dist, catalog_payload,
                tns=tns_info.get('tns'),
                tns_checked=tns_info.get('tns_checked', False),
                tns_snapshot_epoch=tns_info.get('tns_snapshot_epoch'),
            )
        except SoftTimeLimitExceeded:
            # The batch soft time limit can fire mid-row-build; it must not
            # be swallowed as an unbuildable row -- re-raise so the outer
            # handler reverts the batch to INGESTED for re-dispatch (R4).
            raise
        except Exception:
            logger.exception('Skipping unbuildable match row',
                             catalog=catalog_name)
            continue
        records.append(MatchRecord(
            dia_object_id=dia_id,
            catalog_name=catalog_name,
            source_id=src_id,
            dist_arcsec=dist,
            source_ra_deg=ra,
            source_dec_deg=dec,
            catalog_payload=catalog_payload,
            published_payload=published_payload,
        ))
    return records


def compute_crossmatch(alert_rows, now=None, on_tns=None, on_catalog=None):
    """Crossmatch alerts against every configured catalog, in memory.

    This is the compute step shared by ``crossmatch_batch`` and the replay tool:
    it builds the alert frame, computes TNS enrichment, crossmatches each catalog
    (classifying no-overlap and transient skips), and builds the stored and
    published payloads. It writes nothing itself; production persists through the
    callbacks, which run at the same points the batch used to write (KTD1).

    Args:
        alert_rows: ``(uuid, diaObjectId, ra_deg, dec_deg)`` tuples, the shape of
            ``Alert.objects.values_list(...)``.
        now: Override for the current time (TNS currency; tests and replay).
        on_tns: Called with the :class:`TnsResult` before any catalog is read.
        on_catalog: Called as ``on_catalog(name, outcome, records)`` after each
            catalog, in configured order.

    Returns:
        The :class:`CrossmatchResult`.

    Raises:
        RuntimeError: When every catalog's read failed (the >=1-success guard).
        Exception: A deterministic (non-transient) catalog error, re-raised so the
            misconfiguration is surfaced rather than silently skipped.
    """
    alerts_df = pd.DataFrame(
        list(alert_rows),
        columns=['uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg'],
    )
    # Convert UUID objects to strings so PyArrow can serialize them
    alerts_df['uuid'] = alerts_df['uuid'].astype(str)
    result = CrossmatchResult(alert_count=len(alerts_df))
    if alerts_df.empty:
        return result

    clean_df = alerts_df.dropna(subset=['ra_deg', 'dec_deg'])
    result.crossmatched_count = len(clean_df)
    if clean_df.empty:
        return result

    # Build LSDB alerts catalog once, reuse for all reference catalogs
    alerts_catalog = lsdb.from_dataframe(
        clean_df, ra_column='ra_deg', dec_column='dec_deg'
    )

    # Per-alert TNS association, computed once before the catalog loop and
    # replicated across every catalog match for the alert. Best-effort: a
    # missing/stale snapshot or any error yields no tns block (R8), never
    # aborting the batch.
    result.tns = _compute_tns_enrichment(clean_df, now=now)
    if on_tns is not None:
        on_tns(result.tns)

    # Crossmatch against each configured catalog sequentially. Best-effort
    # resilience (R1/R2): one persistently-failing catalog is skipped, not fatal.
    # The >=1-success guard (R3) below still fails the whole batch closed when
    # EVERY catalog errored, so a broad outage reverts instead of publishing empty
    # crossmatches.
    for catalog_config in settings.CROSSMATCH_CATALOGS:
        catalog_name = catalog_config['name']
        records = []
        try:
            result_df = crossmatch_alerts(alerts_catalog, catalog_config)
        except SoftTimeLimitExceeded:
            # The batch soft time limit must self-heal by reverting (R4), never
            # be misread as a catalog skip. It can fire while a transient read
            # error is being handled inside _read_with_retry, which implicitly
            # chains it (SoftTimeLimitExceeded.__context__ = the transient exc);
            # the message-based transient classifier below would then walk the
            # chain and skip the catalog. Match by type here, ahead of that.
            raise
        except Exception as exc:
            # No spatial overlap is normal, not an error: the batch footprint
            # misses this catalog's footprint (e.g. DES's southern-only sky).
            # Counts as a success -- the catalog was read, it just has nothing
            # here -- so it must not trip the >=1-success guard below.
            if (isinstance(exc, RuntimeError)
                    and "Catalogs do not overlap" in str(exc)):
                logger.info('No spatial overlap with catalog',
                            catalog=catalog_name, total=len(clean_df))
                outcome = CATALOG_NO_OVERLAP
            # Decide skip-vs-fail-loud by the transient classification, not by
            # exception type. A DETERMINISTIC error -- a bad/missing/colliding
            # column raised by _get_catalog (ValueError), or a dependency/
            # version-skew mismatch -- must still fail loud so the batch reverts
            # and the misconfiguration is surfaced, rather than silently dropping
            # that catalog from every future batch.
            elif not is_transient_read_error(exc):
                logger.exception('Crossmatch failed for catalog',
                                 catalog=catalog_name)
                raise
            else:
                # A transient read failure whose retries in matching/catalog.py
                # are exhausted (a source host that stays down under load). Skip
                # this catalog and continue rather than aborting the whole batch
                # and rolling back the catalogs that DID succeed (R1). The skip is
                # marked in the published payload (R4).
                logger.warning('Catalog skipped after transient read failure',
                               catalog=catalog_name, error=str(exc))
                outcome = CATALOG_SKIPPED
        else:
            if result_df.empty:
                logger.info('No matches found',
                            catalog=catalog_name, total=len(clean_df))
                outcome = CATALOG_EMPTY
            else:
                outcome = CATALOG_MATCHED
                records = _catalog_records(
                    result_df, catalog_config, result.tns.enrichment
                )

        result.catalog_outcomes[catalog_name] = outcome
        result.records.extend(records)
        if on_catalog is not None:
            on_catalog(catalog_name, outcome, records)

    skipped = result.skipped_catalogs

    # >=1-success guard (R3): if EVERY catalog's read errored (a broad outage,
    # not real "no matches"), fail closed so the caller reverts rather than
    # finalizing alerts with zero matches. A skipped catalog does not count as a
    # success (KTD5).
    if len(skipped) == len(result.catalog_outcomes):
        raise RuntimeError(
            f'All {len(settings.CROSSMATCH_CATALOGS)} catalogs failed to read '
            f'for this batch; reverting rather than publishing empty '
            f'crossmatches (skipped={skipped})'
        )

    # Mark coverage (R4): stamp each published payload with the catalogs skipped
    # in this batch so a consumer can tell what the crossmatch covered. The full
    # skipped set is only known now -- a later catalog can fail after an earlier
    # one's payloads were built -- so stamp after the loop. (No-skip batches keep
    # the build-time default: catalogs_skipped=[], partial=False.)
    if skipped:
        for record in result.records:
            record.published_payload['catalogs_skipped'] = skipped
            record.published_payload['partial'] = True

    return result


# soft_time_limit reverts an overrunning batch via the on-raise path (self-heal,
# R3/R4); time_limit is the SIGKILL backstop for a Dask call that never returns to
# Python for the soft signal. Both are env-configurable settings that MUST stay
# below CROSSMATCH_BATCH_STUCK_SECONDS (the ordering constraint documented there)
# so a live batch self-reverts before the recovery timer could reclaim it.
@shared_task(
    name="crossmatch_batch",
    soft_time_limit=settings.CROSSMATCH_BATCH_SOFT_TIME_LIMIT_SECONDS,
    time_limit=settings.CROSSMATCH_BATCH_TIME_LIMIT_SECONDS,
)
def crossmatch_batch(batch_ids: list, match_version: int = 1) -> None:
    """Process a batch of alerts through LSDB crossmatch against all catalogs.

    Args:
        batch_ids: List of alert UUID strings passed from the dispatcher.
        match_version: Schema version for match results.
    """
    if not batch_ids:
        logger.info('No batch IDs provided')
        return

    logger.info('Starting crossmatch batch',
                batch_size=len(batch_ids), match_version=match_version,
                catalogs=len(settings.CROSSMATCH_CATALOGS))
    def _persist_catalog(catalog_name, outcome, records):
        # Production-only side effects per catalog, at the point the catalog
        # finishes: skips are counted for operators (R5), and matches are
        # committed in-loop so a revert-then-rerun stays idempotent through
        # unique_catalog_match + ignore_conflicts (KTD7).
        if outcome == CATALOG_SKIPPED:
            CATALOG_SKIPS.labels(catalog=catalog_name).inc()
        if outcome != CATALOG_MATCHED:
            return
        CatalogMatch.objects.bulk_create(
            [
                CatalogMatch(
                    alert_id=record.dia_object_id,
                    catalog_name=record.catalog_name,
                    catalog_source_id=record.source_id,
                    match_distance_arcsec=record.dist_arcsec,
                    source_ra_deg=record.source_ra_deg,
                    source_dec_deg=record.source_dec_deg,
                    catalog_payload=record.catalog_payload,
                    match_version=match_version,
                )
                for record in records
            ],
            batch_size=5000, ignore_conflicts=True,
        )
        CROSSMATCH_MATCHES.labels(catalog=catalog_name).inc(len(records))
        logger.info('Wrote matches, queued notifications',
                    catalog=catalog_name,
                    matched=len(records), total=crossmatched)

    crossmatched = 0
    try:
        # 1. Load alerts (once for all catalogs)
        alert_rows = list(Alert.objects.filter(pk__in=batch_ids).values_list(
            'uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg'
        ))
        if not alert_rows:
            logger.warning('No alerts found for batch IDs',
                           batch_size=len(batch_ids))
            return
        crossmatched = sum(
            1 for _, _, ra, dec in alert_rows
            if not (pd.isna(ra) or pd.isna(dec))
        )
        if not crossmatched:
            logger.warning('No alerts with valid coordinates to crossmatch')
            # Terminal, zero-notification alerts: anchor their retention grace now
            # (they never reach the NOTIFIED transition).
            Alert.objects.filter(pk__in=batch_ids).update(
                status=Alert.Status.MATCHED, notified_at=timezone.now()
            )
            return

        # 2. Compute: TNS association and per-catalog crossmatch. TNS rows are
        # persisted before the catalog loop and each catalog's matches as it
        # finishes, exactly as before the compute/persist split. Notifications
        # are accumulated and created together with the MATCHED status update in
        # one transaction at the end (step 3), so a PENDING notification is never
        # visible to dispatch_notifications before its alert is MATCHED.
        # Otherwise dispatch can send a notification and run its MATCHED-gated
        # transition while the alert is still QUEUED; the transition no-ops and
        # single-match alerts get stuck at MATCHED.
        result = compute_crossmatch(
            alert_rows,
            on_tns=_persist_tns_associations,
            on_catalog=_persist_catalog,
        )
        all_notifications = [
            Notification(
                alert_id=record.dia_object_id,
                destination='hopskotch',
                payload=record.published_payload,
            )
            for record in result.records
        ]

        # 3. Create notifications and transition ALL alerts to MATCHED atomically,
        # so notifications become dispatchable exactly when (not before) their
        # alerts are MATCHED. See the note at step 2.
        with transaction.atomic():
            Notification.objects.bulk_create(
                all_notifications, batch_size=5000
            )
            Alert.objects.filter(pk__in=batch_ids).update(
                status=Alert.Status.MATCHED
            )
            # No-match alerts (zero notifications) are terminal here and never reach
            # the NOTIFIED transition -- anchor their retention grace now. Matched
            # alerts instead get notified_at at the NOTIFIED transition.
            matched_keys = {n.alert_id for n in all_notifications}
            Alert.objects.filter(pk__in=batch_ids).exclude(
                lsst_diaObject_diaObjectId__in=matched_keys
            ).update(notified_at=timezone.now())
        CROSSMATCH_BATCHES.labels(result='completed').inc()
        logger.info('Crossmatch batch complete',
                    batch_size=len(batch_ids),
                    notifications=len(all_notifications),
                    catalogs_succeeded=(
                        len(result.catalog_outcomes) - len(result.skipped_catalogs)
                    ),
                    catalogs_skipped=result.skipped_catalogs)

    except Exception:
        CROSSMATCH_BATCHES.labels(result='failed').inc()
        logger.exception('Crossmatch batch failed, reverting to INGESTED',
                         batch_size=len(batch_ids))
        try:
            Alert.objects.filter(pk__in=batch_ids).update(
                status=Alert.Status.INGESTED, queued_at=None
            )
        except Exception:
            logger.exception('Failed to revert batch status')
        raise
