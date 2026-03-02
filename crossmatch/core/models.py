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
    # TEXT UNIQUE NOT NULL    stable identifier from alert
    lsst_diaObject_diaObjectId = models.TextField(unique=True, null=False)
    # TEXT NULL    candidate identifier
    lsst_diaSource_diaSourceId = models.TextField(null=True)
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
            models.Index(fields=['status']),
        ]


class PlannedPointing(models.Model):
    id = models.BigAutoField(primary_key=True)
    source = models.TextField(null=False, default='heroic')
    heroic_pointing_id = models.TextField(null=True)
    obs_id = models.TextField(null=True)
    target_name = models.TextField(null=True)
    planned = models.BooleanField(null=False, default=True)
    s_ra_deg = models.FloatField(null=False)
    s_dec_deg = models.FloatField(null=False)
    radius_deg = models.FloatField(null=False)
    field_geojson = models.JSONField(null=True)
    t_min_mjd = models.FloatField(null=False)
    t_max_mjd = models.FloatField(null=False)
    t_planning_mjd = models.FloatField(null=True)
    instrument_name = models.TextField(null=True)
    ingest_time = models.DateTimeField(null=False, auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['ingest_time']),
            models.Index(fields=['t_min_mjd', 't_max_mjd']),
        ]


class GaiaMatch(models.Model):
    id = models.BigAutoField(primary_key=True)
    alert = models.ForeignKey(
        Alert,
        to_field='lsst_diaObject_diaObjectId',
        on_delete=models.CASCADE,
        db_column='lsst_diaObject_diaObjectId',
    )
    gaia_source_id = models.BigIntegerField(null=False)
    match_distance_arcsec = models.FloatField(null=False)
    match_score = models.FloatField(null=True)
    gaia_ra_deg = models.FloatField(null=True)
    gaia_dec_deg = models.FloatField(null=True)
    gaia_payload = models.JSONField(null=True)
    match_version = models.IntegerField(null=False, default=1)
    created_at = models.DateTimeField(null=False, auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['alert']),
            models.Index(fields=['gaia_source_id']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['alert', 'gaia_source_id', 'match_version'],
                name='unique_gaia_match',
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
        db_column='lsst_diaObject_diaObjectId',
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
            models.Index(fields=['alert']),
            models.Index(fields=['state']),
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
        db_column='lsst_diaObject_diaObjectId',
    )
    gaia_match = models.ForeignKey(
        GaiaMatch,
        on_delete=models.SET_NULL,
        null=True,
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
            models.Index(fields=['state']),
            models.Index(fields=['alert']),
        ]
