"""Shared alert ingest helper used by all broker consumers."""

from tasks.crossmatch import crossmatch_alert
from core.models import Alert, AlertDelivery
from core.log import get_logger

logger = get_logger(__name__)


def ingest_alert(canonical: dict, broker: str) -> bool:
    """Two-step atomic ingest gate (§5.3).

    Step 1: upsert the Alert row by lsst_diaObject_diaObjectId.
    Step 2: record delivery for this broker; if already recorded, skip.
    On first delivery, dispatch the crossmatch Celery task.

    Returns True if the crossmatch task was dispatched, False if skipped.

    canonical keys:
        lsst_diaObject_diaObjectId, ra_deg, dec_deg,
        lsst_diaSource_diaSourceId, event_time, payload
    """
    alert_id = canonical['lsst_diaObject_diaObjectId']
    alert_obj, _ = Alert.objects.get_or_create(
        lsst_diaObject_diaObjectId=alert_id,
        defaults=dict(
            ra_deg=canonical['ra_deg'],
            dec_deg=canonical['dec_deg'],
            lsst_diaSource_diaSourceId=canonical.get('lsst_diaSource_diaSourceId'),
            event_time=canonical['event_time'],
            payload=canonical['payload'],
            status=Alert.Status.INGESTED,
        ),
    )
    _, created = AlertDelivery.objects.get_or_create(
        alert=alert_obj,
        broker=broker,
    )
    if not created:
        logger.info(
            'alert already delivered by this broker, skipping',
            alert_id=alert_id,
            broker=broker,
        )
        return False
    logger.info(f'New alert ingested: {alert_obj}')
    logger.debug(f'Launching crossmatching task for alert {alert_obj}...')
    crossmatch_alert.delay(lsst_diaObject_diaObjectId=alert_obj.lsst_diaObject_diaObjectId)
    return True
