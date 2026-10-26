"""Alert ingest decodes under pandas 3 (lsdb 0.11 upgrade plan U3, R13).

The replay only exercises the crossmatch step, and alerts are paused, so these
tests are the pandas 3 coverage for ingest. Each drives the broker client's own
decoding the way the consumer receives an alert, then the normalizer and
ingest_alert. Fixtures are schema-faithful rather than recorded: retention has
nulled stored broker payloads.
"""

import json
import zlib
from pathlib import Path
from unittest import mock

import pytest

from brokers import ingest_alert
from brokers.normalize import normalize_antares, normalize_lasair, normalize_pittgoogle
from core.models import Alert

FIXTURES = Path(__file__).parent / "fixtures" / "brokers"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


class _KafkaMessage:
    """The two methods antares-client's stream decoder calls on a Kafka message."""

    def __init__(self, value):
        self._value = value

    def error(self):
        return None

    def value(self):
        return self._value


def _antares_locus(message=None):
    """Decode a locus message through antares-client's stream decoder."""
    from antares_client import stream

    if message is None:
        message = _fixture("antares_locus.json")
    raw = zlib.compress(stream.bson.dumps(message))
    return stream._parse_message(_KafkaMessage(raw))


def _antares_alerts(name="antares_alerts.json"):
    """Build the client's Alert models from an alerts fixture (API order)."""
    from antares_client.models import Alert as AntaresAlert

    return [
        AntaresAlert(
            alert_id=item["id"],
            mjd=item["attributes"]["mjd"],
            properties=item["attributes"]["properties"],
        )
        for item in _fixture(name)["data"]
    ]


def _no_network():
    """Fail any HTTP request the ANTARES client would make."""
    return mock.patch(
        "httpx.Client.send", side_effect=AssertionError("unexpected HTTP request")
    )


@pytest.mark.django_db
def test_antares_locus_decodes_normalizes_and_ingests():
    locus = _antares_locus()

    with _no_network(), mock.patch(
        "antares_client.search.get_locus_alerts", return_value=_antares_alerts()
    ) as fetch:
        raw = locus.alerts[0].properties

    fetch.assert_called_once_with("ANT2026abcdefg")
    canonical = normalize_antares(raw)
    assert canonical["lsst_diaObject_diaObjectId"] == 170433143923278190
    assert canonical["ra_deg"] == 238.986105
    assert ingest_alert(canonical, "antares") is True
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=170433143923278190)
    assert alert.lsst_diaSource_diaSourceId == 170433143923278191


def test_antares_newer_stream_format_decodes():
    """2.x also decodes the top-level message shape that 1.14 rejected."""
    attributes = _fixture("antares_locus.json")["data"]["attributes"]
    message = {
        "id": "ANT2026abcdefg",
        "ra": attributes["ra"],
        "dec": attributes["dec"],
        "properties": attributes["properties"],
        "tags": [],
        "catalogs": [],
    }

    locus = _antares_locus(message)

    assert locus.locus_id == "ANT2026abcdefg"
    assert (locus.ra, locus.dec) == (attributes["ra"], attributes["dec"])
    assert locus.properties == attributes["properties"]


@pytest.mark.django_db
def test_pittgoogle_alert_decodes_normalizes_and_ingests():
    import pittgoogle
    from google.cloud.pubsub_v1.types import PubsubMessage

    payload = _fixture("pittgoogle_alert.json")
    msg = PubsubMessage(
        data=json.dumps(payload).encode(),
        attributes={"diaObject_diaObjectId": str(payload["diaObject"]["diaObjectId"])},
    )
    alert = pittgoogle.Alert.from_msg(msg, schema_name="default")

    canonical = normalize_pittgoogle(alert)

    assert canonical["lsst_diaObject_diaObjectId"] == 170433143923278192
    assert canonical["reliability"] == 0.81
    assert ingest_alert(canonical, "pittgoogle") is True
    assert Alert.objects.filter(lsst_diaObject_diaObjectId=170433143923278192).exists()


@pytest.mark.django_db
def test_lasair_alert_decodes_normalizes_and_ingests():
    raw = json.loads((FIXTURES / "lasair_alert.json").read_bytes())

    canonical = normalize_lasair(raw)

    assert canonical["lsst_diaObject_diaObjectId"] == 170433143923278194
    assert canonical["dec_deg"] == -45.25
    assert ingest_alert(canonical, "lasair") is True
    assert Alert.objects.filter(lsst_diaObject_diaObjectId=170433143923278194).exists()
