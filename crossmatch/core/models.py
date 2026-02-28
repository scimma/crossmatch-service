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
