from django.db import models
from django.utils.translation import gettext_lazy as _
from uuid import uuid4
from core.log import get_logger
logger = get_logger(__name__)


class Alert(models.Model):
    class Status(models.TextChoices):
        INGESTED = 'INGESTED', _('ingested')
        QUEUED = 'QUEUED', _('queued')
        MATCHED = 'MATCHED', _('matched')
        NOTIFIED = 'NOTIFIED', _('notified')

    def __str__(self):
        return (f'object_id: {self.lsst_diaObject_diaObjectId}, '
                f'(RA, Dec): ({self.ra_deg}, {self.dec_deg})')

    # Internal UUID
    uuid = models.UUIDField(
        default=uuid4,
        unique=True,
        db_index=True,
        primary_key=True
    )
    # BIGINT UNIQUE NOT NULL    stable identifier from alert
    lsst_diaObject_diaObjectId = models.BigIntegerField(unique=True, null=False, db_column='lsst_diaobject_diaobjectid')
    # BIGINT NULL    candidate identifier
    lsst_diaSource_diaSourceId = models.BigIntegerField(null=True, db_column='lsst_diasource_diasourceid')
    # DOUBLE PRECISION NOT NULL    normalized
    ra_deg = models.FloatField(null=False)
    # DOUBLE PRECISION NOT NULL    normalized
    dec_deg = models.FloatField(null=False)
    # TIMESTAMPTZ NOT NULL    candidate/observation time
    event_time = models.DateTimeField(null=False)
    # TIMESTAMPTZ NOT NULL DEFAULT now()
    ingest_time = models.DateTimeField(null=False, auto_now_add=True)
    # INTEGER NOT NULL    alert schema version
    schema_version = models.IntegerField(null=False, default=1)
    # JSONB NULL    raw payload; nulled by the retention sweep after the grace period
    # once the alert is terminal (its result lives in catalog_matches /
    # core_notification). NULL means the payload has been reclaimed. See
    # tasks/retention.py.
    payload = models.JSONField(null=True)
    # TEXT NOT NULL DEFAULT 'ingested'    ingested, queued, matched, notified
    status = models.TextField(
        choices=Status.choices,
        default=Status.INGESTED,
        null=False,
    )
    # TIMESTAMPTZ NULL    set when the alert enters QUEUED (a batch is dispatched
    # for it); used by dispatch_crossmatch_batch to detect a batch whose worker
    # was killed. Distinct from ingest_time, which is when the alert first
    # arrived and may be far older than when its batch was actually dispatched.
    queued_at = models.DateTimeField(null=True, blank=True)
    # TIMESTAMPTZ NULL    set when the alert reaches a terminal state — NOTIFIED for
    # matched alerts, crossmatch-completion for no-match alerts (which never reach
    # NOTIFIED). Anchors the payload-retention grace period; NULL means the alert is
    # still in flight and its payload is retained regardless of age.
    notified_at = models.DateTimeField(null=True, blank=True)
    # DOUBLE PRECISION NULL    LSST real/bogus score, captured first-seen (read model)
    reliability = models.FloatField(null=True)
    # BIGINT NULL    HEALPix NESTED pixel (order 16) from ra_deg/dec_deg (read model)
    healpix_ipix = models.BigIntegerField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=['status'], name='core_alert_status_idx'),
            models.Index(fields=['reliability'], name='core_alert_reliability_idx'),
            models.Index(fields=['event_time'], name='core_alert_event_time_idx'),
            models.Index(fields=['healpix_ipix'], name='core_alert_healpix_ipix_idx'),
            models.Index(fields=['ingest_time'], name='core_alert_ingest_time_idx'),
            # Partial index for the retention sweep: only rows still carrying a
            # payload are candidates to null. Keeps the index small as payloads
            # are reclaimed.
            models.Index(
                fields=['notified_at'],
                name='core_alert_notified_at_idx',
                condition=models.Q(payload__isnull=False),
            ),
        ]


class AlertDelivery(models.Model):
    """One row per broker per alert — idempotency gate for multi-broker ingest (§5.2.1b)."""
    id = models.BigAutoField(primary_key=True)
    alert = models.ForeignKey(
        Alert,
        to_field='lsst_diaObject_diaObjectId',
        on_delete=models.CASCADE,
        db_column='lsst_diaobject_diaobjectid',
    )
    # 'antares' | 'lasair'
    broker = models.TextField(null=False)
    ingest_time = models.DateTimeField(null=False, auto_now_add=True)

    class Meta:
        db_table = 'alert_deliveries'
        indexes = [
            models.Index(fields=['alert'], name='core_ad_alert_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['alert', 'broker'],
                name='unique_alert_delivery',
            )
        ]


class CatalogMatch(models.Model):
    """Crossmatch results for any HATS catalog (Gaia, DES, SkyMapper, PS1, etc.)."""
    id = models.BigAutoField(primary_key=True)
    alert = models.ForeignKey(
        Alert,
        to_field='lsst_diaObject_diaObjectId',
        on_delete=models.CASCADE,
        db_column='lsst_diaobject_diaobjectid',
    )
    # e.g. 'gaia_dr3', 'des_dr2', 'ps1_dr2'
    catalog_name = models.TextField(null=False)
    # Source identifier in the named catalog (TEXT for universal compatibility)
    catalog_source_id = models.TextField(null=False)
    match_distance_arcsec = models.FloatField(null=False)
    match_score = models.FloatField(null=True)
    source_ra_deg = models.FloatField(null=True)
    source_dec_deg = models.FloatField(null=True)
    catalog_payload = models.JSONField(null=True)
    match_version = models.IntegerField(null=False, default=1)
    created_at = models.DateTimeField(null=False, auto_now_add=True)

    class Meta:
        db_table = 'catalog_matches'
        indexes = [
            models.Index(fields=['alert'], name='core_cm_alert_idx'),
            models.Index(fields=['catalog_name'], name='core_cm_catalog_name_idx'),
            models.Index(fields=['catalog_source_id'], name='core_cm_catalog_source_id_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['alert', 'catalog_name', 'catalog_source_id', 'match_version'],
                name='unique_catalog_match',
            )
        ]


class CrossmatchRun(models.Model):
    class State(models.TextChoices):
        QUEUED = 'queued', _('queued')
        RUNNING = 'running', _('running')
        SUCCEEDED = 'succeeded', _('succeeded')
        FAILED = 'failed', _('failed')

    id = models.BigAutoField(primary_key=True)
    alert = models.ForeignKey(
        Alert,
        to_field='lsst_diaObject_diaObjectId',
        on_delete=models.CASCADE,
        db_column='lsst_diaobject_diaobjectid',
    )
    match_version = models.IntegerField(null=False, default=1)
    celery_task_id = models.TextField(null=True)
    state = models.TextField(
        choices=State.choices,
        default=State.QUEUED,
        null=False,
    )
    attempts = models.IntegerField(null=False, default=0)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    last_error = models.TextField(null=True)
    created_at = models.DateTimeField(null=False, auto_now_add=True)
    updated_at = models.DateTimeField(null=False, auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['alert'], name='core_cmr_alert_idx'),
            models.Index(fields=['state'], name='core_cmr_state_idx'),
        ]


class Notification(models.Model):
    class State(models.TextChoices):
        PENDING = 'pending', _('pending')
        SENT = 'sent', _('sent')
        FAILED = 'failed', _('failed')

    id = models.BigAutoField(primary_key=True)
    alert = models.ForeignKey(
        Alert,
        to_field='lsst_diaObject_diaObjectId',
        on_delete=models.CASCADE,
        db_column='lsst_diaobject_diaobjectid',
    )
    catalog_match = models.ForeignKey(
        CatalogMatch,
        on_delete=models.SET_NULL,
        null=True,
        db_column='catalog_match_id',
    )
    destination = models.TextField(null=False)
    # JSONB NULL    published payload; nulled by the retention sweep after the grace
    # once the notification is SENT (anchor: sent_at). PENDING/FAILED keep it.
    payload = models.JSONField(null=True)
    state = models.TextField(
        choices=State.choices,
        default=State.PENDING,
        null=False,
    )
    attempts = models.IntegerField(null=False, default=0)
    last_error = models.TextField(null=True)
    created_at = models.DateTimeField(null=False, auto_now_add=True)
    updated_at = models.DateTimeField(null=False, auto_now=True)
    sent_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=['state'], name='core_notif_state_idx'),
            models.Index(fields=['alert'], name='core_notif_alert_idx'),
            # Mirror of the alert-side retention index: the sweep filters on
            # sent_at among rows still carrying a payload.
            models.Index(
                fields=['sent_at'],
                name='core_notif_sent_at_idx',
                condition=models.Q(payload__isnull=False),
            ),
        ]


class TnsObject(models.Model):
    """One row per TNS (Transient Name Service) public-catalog object.

    The locally-held snapshot the crossmatch task associates alerts against
    (plan U1/KTD3). Refreshed by the ``refresh_tns_snapshot`` Beat task, which
    seeds from TNS's daily full export and merges its hourly deltas, upserting
    by ``objid``. ``healpix_ipix`` mirrors ``Alert.healpix_ipix`` (NESTED order
    16) so a cone pre-filter can use the same ``BETWEEN`` predicate.
    """
    # BIGINT UNIQUE NOT NULL    stable internal TNS object id; the upsert key
    objid = models.BigIntegerField(unique=True, null=False)
    # TEXT NOT NULL    bare designation, e.g. '2024xyz' (the object-page URL key)
    name = models.TextField(null=False)
    # TEXT NULL    'AT' | 'SN' | ...    prefix split out of the designation
    name_prefix = models.TextField(null=True)
    # DOUBLE PRECISION NOT NULL
    ra_deg = models.FloatField(null=False)
    # DOUBLE PRECISION NOT NULL
    dec_deg = models.FloatField(null=False)
    # TEXT NULL    human classification string, e.g. 'SN Ia' (absent when unclassified)
    type = models.TextField(null=True)
    # DOUBLE PRECISION NULL    redshift when TNS has one
    redshift = models.FloatField(null=True)
    # BIGINT NULL    HEALPix NESTED pixel (order 16) from ra_deg/dec_deg
    healpix_ipix = models.BigIntegerField(null=True)
    updated_at = models.DateTimeField(null=False, auto_now=True)

    class Meta:
        db_table = 'tns_objects'
        indexes = [
            models.Index(fields=['healpix_ipix'], name='core_tns_healpix_ipix_idx'),
        ]


class TnsSnapshotMeta(models.Model):
    """Single-row bookkeeping for the TNS snapshot's freshness.

    Authoritative "snapshot as of" timestamp read by the crossmatch task to
    decide snapshot currency (plan U1 step 3 / U7). Deliberately not derived
    from ``max(TnsObject.updated_at)``, which reflects only the last delta's
    subset of objects, not a whole-snapshot timestamp. Maintained as a single
    row (``pk=1``).
    """
    # TIMESTAMPTZ NULL    end of the last successful refresh; NULL means no snapshot yet
    last_refresh_epoch = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(null=False, auto_now=True)

    class Meta:
        db_table = 'tns_snapshot_meta'


class TnsAssociation(models.Model):
    """Per-alert TNS association result, persisted at crossmatch-build time.

    Lives outside the retention-nulled ``Alert.payload`` / ``Notification.payload``
    so the read-model API ``full`` level can reconstruct the ``tns`` block
    (plan U7/U8/KTD2). ``checked`` plus ``snapshot_epoch`` power R7's enrichment
    indicator (distinguish "no counterpart" from "not checked / stale"). The
    match fields are null when the alert was checked against a current snapshot
    but had no TNS object within the radius. The ``on_delete=CASCADE`` FK bounds
    growth: a row is reclaimed when its alert is deleted.
    """
    # OneToOne on the alert's stable diaObjectId (not the uuid pk) — one
    # association per alert, matching how CatalogMatch/Notification relate.
    alert = models.OneToOneField(
        Alert,
        to_field='lsst_diaObject_diaObjectId',
        on_delete=models.CASCADE,
        db_column='lsst_diaobject_diaobjectid',
    )
    # BOOLEAN NOT NULL    True iff a current snapshot was available at build time
    checked = models.BooleanField(null=False, default=False)
    # TIMESTAMPTZ NULL    the snapshot epoch used for this association
    snapshot_epoch = models.DateTimeField(null=True)
    # Match fields — all NULL when checked but no TNS object within the radius.
    objid = models.BigIntegerField(null=True)
    name = models.TextField(null=True)
    name_prefix = models.TextField(null=True)
    type = models.TextField(null=True)
    redshift = models.FloatField(null=True)
    separation_arcsec = models.FloatField(null=True)
    created_at = models.DateTimeField(null=False, auto_now_add=True)
    updated_at = models.DateTimeField(null=False, auto_now=True)

    class Meta:
        db_table = 'tns_associations'
