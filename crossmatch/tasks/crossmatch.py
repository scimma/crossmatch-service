from celery import shared_task
from core.models import Alert
from core.log import get_logger
logger = get_logger(__name__)


@shared_task(name="crossmatch_batch")
def crossmatch_batch(match_version: int = 1) -> None:
    """Process a batch of QUEUED alerts through LSDB crossmatch.

    Called by dispatch_crossmatch_batch after transitioning alerts from
    INGESTED to QUEUED. On success, transitions to MATCHED. On failure,
    reverts to INGESTED for retry in the next batch.
    """
    alert_ids = list(
        Alert.objects.filter(status=Alert.Status.QUEUED)
        .values_list('pk', flat=True)
    )
    if not alert_ids:
        logger.info('No QUEUED alerts to process')
        return

    logger.info('Starting crossmatch batch',
                batch_size=len(alert_ids), match_version=match_version)
    try:
        # --- LSDB crossmatch stub ---
        for alert in Alert.objects.filter(pk__in=alert_ids).iterator():
            logger.debug('Would crossmatch alert',
                         diaObjectId=alert.lsst_diaObject_diaObjectId)
        # --- end stub ---

        Alert.objects.filter(pk__in=alert_ids).update(
            status=Alert.Status.MATCHED
        )
        logger.info('Crossmatch batch complete', batch_size=len(alert_ids))
    except Exception:
        logger.exception('Crossmatch batch failed, reverting to INGESTED',
                         batch_size=len(alert_ids))
        try:
            Alert.objects.filter(pk__in=alert_ids).update(
                status=Alert.Status.INGESTED
            )
        except Exception:
            logger.exception('Failed to revert batch status')
        raise
