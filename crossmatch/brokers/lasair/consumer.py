"""Lasair Kafka alert consumer.

Lasair broker: lasair-lsst-kafka.lsst.ac.uk:9092
Topic: lasair_366SCiMMA_reliability_moderate
Auth: no credentials required for ingest path
Filter: reliability_moderate — latestR > 0.6, nDiaSources >= 1, lastDiaSource < 1 day ago
Columns: diaObjectId, firstDiaSourceMjdTai, ra, decl
"""
import json
import time

from django.conf import settings
from lasair import lasair_consumer as make_consumer
from brokers import ingest_alert
from brokers.normalize import normalize_lasair
from core.log import get_logger

logger = get_logger(__name__)

BROKER_NAME = 'lasair'
_BACKOFF_INITIAL = 1    # seconds
_BACKOFF_MAX = 60       # seconds


def consume_alerts():
    """Connect to the Lasair Kafka broker and ingest alerts in a poll loop.

    Poll timeout (no messages) is treated as a healthy connection and resets
    backoff. Exceptions (including Kafka-level errors) trigger exponential
    backoff starting at 1 s, doubling up to 60 s.
    """
    logger.info(
        'Connecting to Lasair Kafka broker...',
        host=settings.LASAIR_KAFKA_SERVER,
        topic=settings.LASAIR_TOPIC,
        group_id=settings.LASAIR_GROUP_ID,
    )
    consumer = make_consumer(
        host=settings.LASAIR_KAFKA_SERVER,
        group_id=settings.LASAIR_GROUP_ID,
        topic_in=settings.LASAIR_TOPIC,
    )
    backoff = _BACKOFF_INITIAL
    logger.info('Listening for Lasair alerts...')
    while True:
        try:
            msg = consumer.poll(timeout=20)
            if msg is None:
                # Normal: no messages available in the poll window
                backoff = _BACKOFF_INITIAL
                continue
            if msg.error():
                raise Exception(f'Kafka error: {msg.error()}')
            raw = json.loads(msg.value())
            canonical = normalize_lasair(raw)
            ingest_alert(canonical, broker=BROKER_NAME)
            backoff = _BACKOFF_INITIAL
        except Exception as err:
            logger.error(f'Error consuming Lasair alert: {err}')
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
