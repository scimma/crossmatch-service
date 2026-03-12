"""ANTARES streaming alert consumer.

Connects to the ANTARES alert broker via the antares-client StreamingClient
and ingests alerts into the crossmatch pipeline.
"""

import time

from antares_client import StreamingClient
from django.conf import settings
from brokers import ingest_alert
from brokers.normalize import normalize_antares
from core.log import get_logger

logger = get_logger(__name__)

BROKER_NAME = 'antares'
_BACKOFF_INITIAL = 1    # seconds
_BACKOFF_MAX = 60       # seconds


def consume_alerts():
    """Connect to the ANTARES streaming broker and ingest alerts.

    Uses StreamingClient.iter() which yields (topic, locus) tuples.
    On error, reconnects with exponential backoff matching the Lasair
    consumer pattern.
    """
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            logger.info(
                'Connecting to ANTARES streaming broker...',
                topic=settings.ANTARES_TOPIC,
                group_id=settings.ANTARES_GROUP_ID,
            )
            client = StreamingClient(
                topics=[settings.ANTARES_TOPIC],
                api_key=settings.ANTARES_API_KEY,
                api_secret=settings.ANTARES_API_SECRET,
                group=settings.ANTARES_GROUP_ID,
            )
            logger.info('Listening for ANTARES alerts...')
            for topic, locus in client.iter():
                try:
                    newest_alert = locus.alerts[0]
                    raw = newest_alert.properties
                    if 'lsst_diaObject_diaObjectId' not in raw:
                        logger.info(
                            'Skipping alert without lsst_diaObject_diaObjectId',
                            locus_id=locus.locus_id,
                        )
                        continue
                    canonical = normalize_antares(raw)
                    ingest_alert(canonical, broker=BROKER_NAME)
                    backoff = _BACKOFF_INITIAL
                except Exception as err:
                    logger.error(f'Error ingesting ANTARES alert: {err}')
        except Exception as err:
            logger.error(f'ANTARES streaming error: {err}')
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
