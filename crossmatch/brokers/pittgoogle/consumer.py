"""Pitt-Google Pub/Sub alert consumer.

Pitt-Google broker: Google Cloud Pub/Sub
Topic: lsst-alerts (project: pitt-alert-broker)
Auth: GCP service account (GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS)
Filter: attribute filter 'attributes:diaObject_diaObjectId' applied server-side
"""
import time

import pittgoogle
from django.conf import settings
from brokers import ingest_alert
from brokers.normalize import normalize_pittgoogle
from core.log import get_logger

logger = get_logger(__name__)

BROKER_NAME = 'pittgoogle'
_BACKOFF_INITIAL = 1    # seconds
_BACKOFF_MAX = 60       # seconds


def _msg_callback(alert):
    """Process a single alert from the Pub/Sub stream.

    Ack on success or permanent failure (malformed data).
    Nack only on transient failure (database error) so Pub/Sub redelivers.
    """
    try:
        canonical = normalize_pittgoogle(alert)
    except Exception as err:
        # Permanent failure: malformed alert data will never normalize.
        # Ack to prevent infinite redelivery (matches ANTARES inner try/except
        # pattern where normalization errors are caught and the loop continues).
        logger.error(
            'Failed to normalize Pitt-Google alert, acking to discard',
            error=str(err),
        )
        return pittgoogle.pubsub.Response(ack=True, result=None)

    try:
        ingest_alert(canonical, broker=BROKER_NAME)
    except Exception as err:
        # Transient failure: database error, connection issue, etc.
        # Nack so Pub/Sub redelivers after the ack deadline.
        logger.error(
            'Failed to ingest Pitt-Google alert, nacking for redelivery',
            error=str(err),
            diaObjectId=canonical.get('lsst_diaObject_diaObjectId'),
        )
        return pittgoogle.pubsub.Response(ack=False, result=None)

    return pittgoogle.pubsub.Response(ack=True, result=None)


def consume_alerts():
    """Subscribe to the Pitt-Google lsst-alerts topic and ingest alerts.

    Uses pittgoogle.pubsub.Consumer.stream() which blocks indefinitely,
    dispatching alerts to _msg_callback in a thread pool. On stream failure
    (network error, credential issue), reconnects with exponential backoff
    matching the ANTARES/Lasair consumer pattern.
    """
    topic = pittgoogle.Topic(
        name=settings.PITTGOOGLE_TOPIC,
        projectid=settings.PITTGOOGLE_PUBLISHER_PROJECT,
    )
    subscription = pittgoogle.Subscription(
        name=settings.PITTGOOGLE_SUBSCRIPTION,
        topic=topic,
        schema_name='lsst',
    )

    backoff = _BACKOFF_INITIAL
    while True:
        try:
            logger.info(
                'Creating/verifying Pitt-Google Pub/Sub subscription...',
                subscription=settings.PITTGOOGLE_SUBSCRIPTION,
                topic=settings.PITTGOOGLE_TOPIC,
                publisher_project=settings.PITTGOOGLE_PUBLISHER_PROJECT,
            )
            subscription.touch(attribute_filter='attributes:diaObject_diaObjectId')

            consumer = pittgoogle.pubsub.Consumer(
                subscription=subscription,
                msg_callback=_msg_callback,
            )
            logger.info('Listening for Pitt-Google alerts...')
            consumer.stream()  # blocks indefinitely
        except Exception as err:
            logger.error(f'Pitt-Google streaming error: {err}')
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
