from time import sleep
from core.log import get_logger
logger = get_logger(__name__)


def consume_alerts():
    logger.info('Listening to alert broker...')
    sleep(3600)
