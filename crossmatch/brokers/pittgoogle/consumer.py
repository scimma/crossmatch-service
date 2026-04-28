"""Pitt-Google Pub/Sub alert consumer.

Pitt-Google broker: Google Cloud Pub/Sub
Topic: lsst-alerts-json (project: pitt-alert-broker)
Auth: GCP service account (GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS)
Filters (both server-side, attached to the subscription):
  1. attribute filter 'attributes:diaObject_diaObjectId' (drops alerts without diaObjectId)
  2. SMT JavaScript UDF reliabilityFilter (drops alerts whose latest diaSource
     has reliability < settings.MIN_DIASOURCE_RELIABILITY or missing/null reliability)
  See scimma_crossmatch_service_design.md §2.2 (broker filter standard).

Subscribes to the JSON-formatted topic rather than the Avro lsst-alerts
topic because pittgoogle's LsstSchema assumes Confluent wire format
(5-byte schema-ID prefix), which doesn't match the actual lsst-alerts
payload framing and produces fastavro decode errors.
"""
import time

import pittgoogle
from django.conf import settings
from google.protobuf.field_mask_pb2 import FieldMask
from google.pubsub_v1.types import (
    JavaScriptUDF,
    MessageTransform,
    Subscription as SubscriptionProto,
)

from brokers import ingest_alert
from brokers.normalize import normalize_pittgoogle
from core.log import get_logger

logger = get_logger(__name__)

BROKER_NAME = 'pittgoogle'
_BACKOFF_INITIAL = 1    # seconds
_BACKOFF_MAX = 60       # seconds

# JS function name embedded in the SMT UDF source. pittgoogle's first-create
# path parses the function name from the source via regex
# `function\s+([a-zA-Z0-9_]+)\s*\(`, so the UDF source must declare the
# function with this exact named-declaration form for the create-path
# function_name to match what we pass to JavaScriptUDF on update.
_UDF_FUNCTION_NAME = 'reliabilityFilter'


def _build_reliability_udf(threshold: float) -> str:
    """Return SMT JavaScript UDF source filtering by latest-diaSource reliability.

    The UDF returns null (drop) for messages whose latest diaSource has
    missing/null/non-numeric reliability or reliability below threshold;
    otherwise returns the message unchanged. NaN is rejected because
    `typeof NaN === "number"` is true but `NaN >= anything` is always false.

    Encoding: per Google's Pub/Sub SMT JavaScript UDF contract,
    `message.data` is delivered as a UTF-8 encoded string and must remain
    one on output -- see https://cloud.google.com/pubsub/docs/smts/udfs-overview
    ("data: (String, required) The message payload"; "the payload input
    and output must be UTF-8 encoded strings"). Parse it directly with
    JSON.parse; do not wrap in atob(), which would throw on the first
    non-base64 character of the JSON payload (e.g. `{`) and the catch-all
    below would then drop every message.

    Threshold is interpolated via repr() (lossless for any IEEE-754 double).
    The caller must have bounds-checked the value to a finite float in
    [0.0, 1.0]; settings.MIN_DIASOURCE_RELIABILITY does this at import time.
    """
    return (
        f'function {_UDF_FUNCTION_NAME}(message, metadata) {{\n'
        f'  try {{\n'
        f'    const payload = JSON.parse(message.data);\n'
        f'    const score = payload && payload.diaSource && payload.diaSource.reliability;\n'
        # Inverted comparison handles NaN correctly: NaN >= anything is false,
        # so !(NaN >= threshold) is true -> drop. score < threshold would let
        # NaN pass because NaN < threshold is also false.
        f'    if (typeof score !== "number" || !(score >= {threshold!r})) {{\n'
        f'      return null;\n'
        f'    }}\n'
        f'    return message;\n'
        f'  }} catch (e) {{\n'
        f'    return null;\n'
        f'  }}\n'
        f'}}\n'
    )


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
        schema_name='default',
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
