import json
from celery import shared_task
from core.models import Alert
from core.log import get_logger
logger = get_logger(__name__)


@shared_task(name="Crossmatch")
def crossmatch(alert_id):
    logger.info(f'Crossmatching alert {alert_id}...')
    alert = Alert.objects.get(uuid=alert_id)
    logger.info(json.dumps(alert.payload, indent=2))
