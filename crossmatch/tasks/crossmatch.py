import json
from celery import shared_task
from core.models import Alert
from core.log import get_logger
logger = get_logger(__name__)


@shared_task(name="crossmatch_alert")
def crossmatch_alert(lsst_diaObject_diaObjectId: int, match_version: int = 1):
    logger.info(f'Crossmatching alert {lsst_diaObject_diaObjectId} (version={match_version})...')
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=lsst_diaObject_diaObjectId)
    logger.info(json.dumps(alert.payload, indent=2))
