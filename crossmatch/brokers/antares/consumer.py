"""ANTARES streaming alert consumer.

Connects to the ANTARES alert broker via the antares-client StreamingClient
and ingests alerts into the crossmatch pipeline.
"""

import time

from antares_client import StreamingClient
from django.conf import settings
from brokers import ingest_alert
from brokers.normalize import normalize_antares, select_antares_rubin_alert
from core.log import get_logger

logger = get_logger(__name__)

BROKER_NAME = 'antares'
_BACKOFF_INITIAL = 1    # seconds
_BACKOFF_MAX = 60       # seconds


def consume_alerts():
    """Connect to the ANTARES streaming broker and ingest alerts.

    Polls StreamingClient for (topic, locus) tuples. A message the client
    cannot decode is skipped on the same client; any other streaming error
    reconnects with exponential backoff matching the Lasair consumer pattern.
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
            while True:
                try:
                    topic, locus = client.poll()
                except ValueError as err:
                    # antares-client 2.x raises ValueError for a message it
                    # cannot decode.
                    logger.warning(
                        'Skipping undecodable ANTARES message', error=str(err)
                    )
                    continue
                try:
                    rubin_alert = select_antares_rubin_alert(locus.alerts)
                    if rubin_alert is None:
                        logger.info(
                            'Skipping alert without lsst_diaObject_diaObjectId',
                            locus_id=locus.locus_id,
                        )
                        continue
                    canonical = normalize_antares(rubin_alert.properties)
                    ingest_alert(canonical, broker=BROKER_NAME)
                    backoff = _BACKOFF_INITIAL
                except Exception as err:
                    logger.error(f'Error ingesting ANTARES alert: {err}')
        except Exception as err:
            logger.error(f'ANTARES streaming error: {err}')
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
