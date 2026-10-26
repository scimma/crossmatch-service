"""ANTARES consumer loop: which alert a streamed locus ingests.

A fake StreamingClient feeds the real consume_alerts() loop. Each locus is
decoded by antares-client's own stream decoder, and the client's search
module (its REST fetch of a locus's alerts) is stubbed from a fixture, so no
Kafka broker or network is involved.
"""

import json
import zlib
from pathlib import Path
from unittest import mock

import pytest

from brokers.antares import consumer
from core.models import AlertDelivery

FIXTURES = Path(__file__).parent / "fixtures" / "brokers"


class _Stop(BaseException):
    """Ends the consumer's infinite loop; BaseException escapes its handlers."""


class _KafkaMessage:
    def __init__(self, value):
        self._value = value

    def error(self):
        return None

    def value(self):
        return self._value


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _locus():
    from antares_client import stream

    raw = zlib.compress(stream.bson.dumps(_fixture("antares_locus.json")))
    return stream._parse_message(_KafkaMessage(raw))


def _alerts(name):
    from antares_client.models import Alert as AntaresAlert

    return [
        AntaresAlert(
            alert_id=item["id"],
            mjd=item["attributes"]["mjd"],
            properties=item["attributes"]["properties"],
        )
        for item in _fixture(name)["data"]
    ]


class _FakeClient:
    """Plays a scripted sequence of poll() outcomes, then stops the loop."""

    instances = []

    def __init__(self, script):
        self._script = list(script)
        _FakeClient.instances.append(self)

    def poll(self, timeout=None):
        if not self._script:
            raise _Stop()
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def iter(self, limit=None):
        while True:
            yield self.poll()


def _run(script, alerts):
    _FakeClient.instances = []
    with mock.patch.object(
        consumer, "StreamingClient", side_effect=lambda **_: _FakeClient(script)
    ), mock.patch(
        "antares_client.search.get_locus_alerts", return_value=alerts
    ), mock.patch.object(
        consumer.time, "sleep"
    ) as sleep:
        with pytest.raises(_Stop):
            consumer.consume_alerts()
    return sleep


@pytest.mark.django_db
def test_ztf_history_locus_ingests_newest_rubin_alert():
    """AE4: a locus with ZTF alerts at both ends still ingests its Rubin object."""
    _run([("topic", _locus())], _alerts("antares_alerts_ztf_history.json"))

    delivery = AlertDelivery.objects.get(broker="antares")
    assert delivery.alert.lsst_diaObject_diaObjectId == 170446371625109043
    assert delivery.alert.lsst_diaSource_diaSourceId == 170525491642502484


@pytest.mark.django_db
def test_locus_without_rubin_alert_is_skipped():
    ztf_only = [
        a
        for a in _alerts("antares_alerts_ztf_history.json")
        if a.alert_id.startswith("ztf")
    ]

    with mock.patch.object(consumer, "ingest_alert") as ingest:
        _run([("topic", _locus())], ztf_only)

    ingest.assert_not_called()
