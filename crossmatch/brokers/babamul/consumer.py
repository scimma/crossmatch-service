"""Babamul Kafka alert consumer.

Babamul broker: kaboom.caltech.edu:9093, SASL_PLAINTEXT with SCRAM-SHA-512.
Each message value is an Avro Object Container File holding one LSST alert,
with its schema embedded. The service applies its own reliability cut
(``MIN_DIASOURCE_RELIABILITY``) because Babamul's topics are split by class,
not by reliability.

Delivery is at-least-once: a message's offset is stored only once it reaches
a final outcome, so a transient database error retries the message in place
and nothing is skipped. Configuration problems (missing credentials, a
rejected login, a topic or group the account may not use) exit non-zero so
the pod restarts visibly instead of idling.
"""

import io
import time

import fastavro
from confluent_kafka import Consumer, KafkaError, TopicPartition
from django.conf import settings
from django.db import InterfaceError, OperationalError

from brokers import ingest_alert
from brokers.normalize import normalize_babamul, passes_reliability_cut
from core.log import get_logger
from core.metrics import BABAMUL_MESSAGES

logger = get_logger(__name__)

BROKER_NAME = 'babamul'
_BACKOFF_INITIAL = 1  # seconds
_BACKOFF_MAX = 60  # seconds
_POLL_TIMEOUT = 20  # seconds

# Errors a rebuild cannot fix: the account or its grants are wrong.
_EXIT_CODES = frozenset(
    {
        KafkaError._AUTHENTICATION,
        KafkaError.TOPIC_AUTHORIZATION_FAILED,
        KafkaError.GROUP_AUTHORIZATION_FAILED,
    }
)


class _Rebuild(Exception):
    """The consumer must be closed and built again after a backoff."""


def group_id(username: str, suffix: str) -> str:
    """The consumer group, prefixed with ``<username>-`` as Babamul requires.

    Args:
        username: The Babamul Kafka username.
        suffix: The configured group suffix (``BABAMUL_GROUP_ID``).

    Returns:
        ``<username>-<suffix>``, or ``suffix`` when it already has the prefix.
    """
    prefix = f'{username}-'
    return suffix if suffix.startswith(prefix) else prefix + suffix


def decode_message(data: bytes) -> dict:
    """Decode one Babamul message: an Avro container file with one record.

    Args:
        data: The Kafka message value.

    Returns:
        The decoded alert record.

    Raises:
        Exception: Any fastavro error for bytes that are not a valid container.
    """
    return next(fastavro.reader(io.BytesIO(data)))


def _exit(reason: str, **fields) -> None:
    logger.error(reason, **fields)
    raise SystemExit(1)


def _check_error(err: KafkaError) -> None:
    """Exit on configuration errors, rebuild on fatal ones, else log and go on."""
    code = err.code()
    if code in _EXIT_CODES:
        _exit(
            'Babamul rejected the consumer configuration',
            error=err.str(),
            code=err.name(),
        )
    if err.fatal() or code == KafkaError._ALL_BROKERS_DOWN:
        raise _Rebuild(err.str())
    if code != KafkaError._PARTITION_EOF:
        logger.warning('Babamul Kafka error', error=err.str(), code=err.name())


def _keep_alive(kafka) -> None:
    """Poll once while partitions are paused, so the group keeps this member.

    A partition assigned by a rebalance during the pause is not paused yet, so
    a message from it is rewound to be read again later, and that partition is
    paused too.
    """
    message = kafka.poll(0)
    if message is None:
        return
    if message.error():
        _check_error(message.error())
        return
    kafka.seek(TopicPartition(message.topic(), message.partition(), message.offset()))
    kafka.pause(kafka.assignment())


def _ingest_with_retry(canonical: dict, kafka) -> bool:
    """Ingest, retrying transient database errors in place until it succeeds.

    While it retries, the assigned partitions are paused and the consumer keeps
    polling. Without the polls, an outage longer than max.poll.interval.ms
    would evict the consumer, and a partition with no committed offset would
    rejoin at the latest offset and skip the alerts that arrived meanwhile.

    Args:
        canonical: The normalized alert.
        kafka: The consumer the alert came from.

    Returns:
        True for a new delivery, False for a repeat.
    """
    backoff = _BACKOFF_INITIAL
    paused = False
    try:
        while True:
            try:
                return ingest_alert(canonical, broker=BROKER_NAME)
            except (OperationalError, InterfaceError) as err:
                logger.warning(
                    'Database unavailable, retrying Babamul alert', error=str(err)
                )
                if not paused:
                    kafka.pause(kafka.assignment())
                    paused = True
                time.sleep(backoff)
                _keep_alive(kafka)
                backoff = min(backoff * 2, _BACKOFF_MAX)
    finally:
        if paused:
            kafka.resume(kafka.assignment())


def _handle(message, kafka) -> str:
    """Process one message and return its final outcome."""
    topic = message.topic()
    try:
        record = decode_message(message.value())
    except Exception as err:
        logger.warning(
            'Skipping undecodable Babamul message', topic=topic, error=str(err)
        )
        return 'undecodable'
    canonical = normalize_babamul(record, topic)
    if canonical is None:
        logger.info(
            'Skipping Babamul alert without diaObjectId or position', topic=topic
        )
        return 'no_object'
    if not passes_reliability_cut(
        canonical['reliability'], settings.MIN_DIASOURCE_RELIABILITY
    ):
        return 'below_cut'
    return 'ingested' if _ingest_with_retry(canonical, kafka) else 'duplicate'


def _consumer_config(errors: list) -> dict:
    username = settings.BABAMUL_KAFKA_USERNAME
    return {
        'bootstrap.servers': settings.BABAMUL_KAFKA_SERVER,
        'group.id': group_id(username, settings.BABAMUL_GROUP_ID),
        'client.id': 'scimma-crossmatch',
        'security.protocol': 'SASL_PLAINTEXT',
        'sasl.mechanism': 'SCRAM-SHA-512',
        'sasl.username': username,
        'sasl.password': settings.BABAMUL_KAFKA_PASSWORD,
        'auto.offset.reset': 'latest',
        'enable.auto.commit': True,
        'enable.auto.offset.store': False,
        'error_cb': errors.append,
    }


def consume_alerts():
    """Consume Babamul's LSST topics and ingest alerts that pass the cut.

    Raises:
        SystemExit: With code 1 on missing configuration or a configuration
            error reported by Babamul.
    """
    for name in (
        'BABAMUL_KAFKA_USERNAME',
        'BABAMUL_KAFKA_PASSWORD',
        'BABAMUL_GROUP_ID',
    ):
        if not getattr(settings, name):
            _exit('Babamul consumer is not configured', missing=name)

    backoff = _BACKOFF_INITIAL
    while True:
        errors = []
        config = _consumer_config(errors)
        kafka = None
        try:
            logger.info(
                'Connecting to Babamul Kafka broker...',
                server=settings.BABAMUL_KAFKA_SERVER,
                group_id=config['group.id'],
                topics=settings.BABAMUL_TOPICS,
            )
            kafka = Consumer(config)
            kafka.subscribe(settings.BABAMUL_TOPICS)
            logger.info('Listening for Babamul alerts...')
            while True:
                message = kafka.poll(_POLL_TIMEOUT)
                while errors:
                    _check_error(errors.pop(0))
                if message is None:
                    backoff = _BACKOFF_INITIAL
                    continue
                if message.error():
                    _check_error(message.error())
                    continue
                backoff = _BACKOFF_INITIAL
                try:
                    outcome = _handle(message, kafka)
                except Exception as err:
                    logger.error(
                        'Error ingesting Babamul alert',
                        topic=message.topic(),
                        partition=message.partition(),
                        offset=message.offset(),
                        error=str(err),
                    )
                    outcome = 'error'
                BABAMUL_MESSAGES.labels(topic=message.topic(), outcome=outcome).inc()
                kafka.store_offsets(message=message)
        except Exception as err:
            logger.error('Babamul consumer error, reconnecting', error=str(err))
            if kafka is not None:
                try:
                    kafka.close()
                except Exception:
                    pass
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
