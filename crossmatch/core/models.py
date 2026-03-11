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
    # JSONB NOT NULL    raw payload
    payload = models.JSONField(null=False)
    # TEXT NOT NULL DEFAULT 'ingested'    ingested, queued, matched, notified
    status = models.TextField(
        choices=Status.choices,
        default=Status.INGESTED,
        null=False,
    )

    class Meta:
        indexes = [
            models.Index(fields=['status'], name='core_alert_status_idx'),
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
    payload = models.JSONField(null=False)
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
        ]
