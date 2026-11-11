"""Babamul consumer loop: per-message outcomes, offsets, errors and backoff.

A fake confluent-kafka Consumer feeds the real consume_alerts() loop with
Avro container bytes built the way Babamul publishes them, so the real
decoder, normalizer and ingest path run with no Kafka broker or network.
"""

from unittest import mock

import pytest
from confluent_kafka import KafkaError
from django.db import OperationalError
from django.test import override_settings
from structlog.testing import capture_logs

from brokers.babamul import consumer
from core.metrics import BABAMUL_MESSAGES
from core.models import Alert, AlertDelivery
from tests.babamul_avro import encode, fixture_record

TOPIC = 'babamul.lsst.ztf-match.hosted'

CREDENTIALS = dict(
    BABAMUL_KAFKA_USERNAME='babamul-scimma',
    BABAMUL_KAFKA_PASSWORD='secret',
    BABAMUL_GROUP_ID='scimma-crossmatch-test',
    MIN_DIASOURCE_RELIABILITY=0.6,
)


class _Stop(BaseException):
    """Ends the consumer's infinite loop; BaseException escapes its handlers."""


class _Message:
    _next_offset = 0

    def __init__(self, value=None, topic=TOPIC, error=None):
        self._value = value
        self._topic = topic
        self._error = error
        _Message._next_offset += 1
        self._offset = _Message._next_offset

    def error(self):
        return self._error

    def value(self):
        return self._value

    def topic(self):
        return self._topic

    def partition(self):
        return 0

    def offset(self):
        return self._offset


def _alert(**candidate):
    return _Message(encode(fixture_record(**candidate)))


def _kafka_error(code, fatal=False):
    return _Message(error=KafkaError(code, fatal=fatal))


class _FakeConsumer:
    """Plays a shared script of poll() results, then stops the loop."""

    instances = []

    def __init__(self, config, script):
        self.config = config
        self.script = script
        self.topics = None
        self.stored = []
        self.closed = False
        _FakeConsumer.instances.append(self)

    def subscribe(self, topics):
        self.topics = topics

    def poll(self, timeout=None):
        if not self.script:
            raise _Stop()
        return self.script.pop(0)

    def store_offsets(self, message=None):
        self.stored.append(message.offset())

    def close(self):
        self.closed = True


def _run(script, expect=_Stop):
    _FakeConsumer.instances = []
    script = list(script)
    with mock.patch.object(
        consumer, 'Consumer', side_effect=lambda config: _FakeConsumer(config, script)
    ), mock.patch.object(consumer.time, 'sleep') as sleep:
        with pytest.raises(expect) as raised:
            consumer.consume_alerts()
    return sleep, raised


def _count(outcome, topic=TOPIC):
    return BABAMUL_MESSAGES.labels(topic=topic, outcome=outcome)._value.get()


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_valid_alert_is_ingested_counted_and_its_offset_stored():
    before = _count('ingested')
    message = _alert()

    _run([None, message])

    assert AlertDelivery.objects.filter(broker='babamul').count() == 1
    assert _count('ingested') == before + 1
    assert _FakeConsumer.instances[0].stored == [message.offset()]


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_consumer_config_group_and_topics():
    _run([])

    fake = _FakeConsumer.instances[0]
    assert fake.config['group.id'] == 'babamul-scimma-scimma-crossmatch-test'
    assert fake.config['security.protocol'] == 'SASL_PLAINTEXT'
    assert fake.config['sasl.mechanism'] == 'SCRAM-SHA-512'
    assert fake.config['auto.offset.reset'] == 'latest'
    assert fake.config['enable.auto.offset.store'] is False
    assert fake.topics == [
        'babamul.lsst.no-ztf-match.hosted',
        'babamul.lsst.no-ztf-match.hostless',
        'babamul.lsst.no-ztf-match.unknown',
        'babamul.lsst.ztf-match.hosted',
        'babamul.lsst.ztf-match.hostless',
        'babamul.lsst.ztf-match.unknown',
    ]


def test_group_id_is_not_prefixed_twice():
    assert consumer.group_id('babamul-scimma', 'x') == 'babamul-scimma-x'
    assert consumer.group_id('babamul-scimma', 'babamul-scimma-x') == 'babamul-scimma-x'


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_undecodable_message_is_skipped_on_the_same_consumer():
    garbage = _Message(b'not avro at all')

    with capture_logs() as logs:
        sleep, _ = _run([garbage, _alert()])

    assert len(_FakeConsumer.instances) == 1
    sleep.assert_not_called()
    assert _FakeConsumer.instances[0].stored[0] == garbage.offset()
    assert AlertDelivery.objects.filter(broker='babamul').count() == 1
    skips = [e for e in logs if 'undecodable' in e['event']]
    assert [e['log_level'] for e in skips] == ['warning']
    assert skips[0]['topic'] == TOPIC


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_alert_below_cut_is_counted_and_not_ingested():
    # Covers AE1: reliability 0.55 against a 0.6 threshold.
    before = _count('below_cut')
    message = _alert(reliability=0.55)

    _run([message])

    assert not Alert.objects.exists()
    assert _count('below_cut') == before + 1
    assert _FakeConsumer.instances[0].stored == [message.offset()]


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_alert_without_dia_object_is_skipped():
    # Covers AE3.
    before = _count('no_object')
    with capture_logs() as logs:
        _run([_alert(diaObjectId=None), _alert()])

    assert _count('no_object') == before + 1
    assert AlertDelivery.objects.filter(broker='babamul').count() == 1
    assert any(e['log_level'] == 'info' and 'diaObjectId' in e['event'] for e in logs)


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_repeat_delivery_is_counted_as_duplicate():
    before = _count('duplicate')

    _run([_alert(), _alert()])

    assert _count('duplicate') == before + 1
    assert len(_FakeConsumer.instances[0].stored) == 2


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_transient_db_error_retries_same_message_without_rebuild():
    message = _alert()
    real_ingest = consumer.ingest_alert
    calls = []

    def flaky(canonical, broker):
        calls.append(broker)
        if len(calls) <= 2:
            raise OperationalError('connection is closed')
        return real_ingest(canonical, broker=broker)

    with mock.patch.object(consumer, 'ingest_alert', side_effect=flaky):
        sleep, _ = _run([message])

    assert len(calls) == 3
    assert len(_FakeConsumer.instances) == 1
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2]
    assert _FakeConsumer.instances[0].stored == [message.offset()]
    assert AlertDelivery.objects.filter(broker='babamul').count() == 1


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_non_transient_ingest_error_is_final_and_processing_continues():
    before = _count('error')
    bad, good = _alert(), _alert(diaObjectId=170433143923278200)
    real_ingest = consumer.ingest_alert

    def boom_once(canonical, broker):
        if canonical['lsst_diaObject_diaObjectId'] == 170433143923278196:
            raise ValueError('bad value')
        return real_ingest(canonical, broker=broker)

    with capture_logs() as logs, mock.patch.object(
        consumer, 'ingest_alert', side_effect=boom_once
    ):
        _run([bad, good])

    assert _count('error') == before + 1
    assert _FakeConsumer.instances[0].stored == [bad.offset(), good.offset()]
    assert AlertDelivery.objects.filter(broker='babamul').count() == 1
    error = next(e for e in logs if e['log_level'] == 'error')
    assert (error['topic'], error['partition'], error['offset']) == (
        TOPIC,
        0,
        bad.offset(),
    )


@pytest.mark.django_db
@override_settings(**CREDENTIALS)
def test_retriable_kafka_errors_keep_polling_on_the_same_consumer():
    script = [
        _kafka_error(KafkaError._TRANSPORT),
        _kafka_error(KafkaError.UNKNOWN_TOPIC_OR_PART),
        _kafka_error(KafkaError._PARTITION_EOF),
        _alert(),
    ]
    sleep, _ = _run(script)

    assert len(_FakeConsumer.instances) == 1
    sleep.assert_not_called()
    assert AlertDelivery.objects.filter(broker='babamul').count() == 1


@override_settings(**CREDENTIALS)
def test_all_brokers_down_rebuilds_with_backoff_that_resets():
    script = [
        _kafka_error(KafkaError._ALL_BROKERS_DOWN),
        _kafka_error(KafkaError._ALL_BROKERS_DOWN, fatal=True),
        None,
        _kafka_error(KafkaError._ALL_BROKERS_DOWN),
    ]
    sleep, _ = _run(script)

    assert len(_FakeConsumer.instances) == 4
    assert all(c.closed for c in _FakeConsumer.instances[:3])
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2, 1]


@pytest.mark.parametrize(
    'code',
    [
        KafkaError._AUTHENTICATION,
        KafkaError.TOPIC_AUTHORIZATION_FAILED,
        KafkaError.GROUP_AUTHORIZATION_FAILED,
    ],
)
@override_settings(**CREDENTIALS)
def test_authentication_and_authorization_errors_exit_non_zero(code):
    with capture_logs() as logs:
        _, raised = _run([_kafka_error(code)], expect=SystemExit)

    assert raised.value.code == 1
    assert any(e['log_level'] == 'error' for e in logs)
    assert 'secret' not in repr(logs)


@override_settings(**CREDENTIALS)
def test_error_callback_authentication_error_exits_non_zero():
    def poll_then_auth_error(fake):
        fake.config['error_cb'](KafkaError(KafkaError._AUTHENTICATION))
        return None

    _FakeConsumer.instances = []
    with mock.patch.object(
        consumer, 'Consumer', side_effect=lambda config: _FakeConsumer(config, [])
    ), mock.patch.object(
        _FakeConsumer, 'poll', lambda self, timeout=None: poll_then_auth_error(self)
    ), pytest.raises(
        SystemExit
    ) as raised:
        consumer.consume_alerts()

    assert raised.value.code == 1


@pytest.mark.parametrize(
    'missing',
    [
        'BABAMUL_KAFKA_USERNAME',
        'BABAMUL_KAFKA_PASSWORD',
        'BABAMUL_GROUP_ID',
    ],
)
def test_missing_configuration_exits_before_connecting(missing):
    with override_settings(**{**CREDENTIALS, missing: ''}), mock.patch.object(
        consumer, 'Consumer'
    ) as kafka, pytest.raises(SystemExit) as raised:
        consumer.consume_alerts()

    assert raised.value.code == 1
    kafka.assert_not_called()
