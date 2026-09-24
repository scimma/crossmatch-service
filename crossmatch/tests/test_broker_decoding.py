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


def _antares_locus():
    """Decode the locus fixture through antares-client's stream decoder."""
    from antares_client import stream

    raw = zlib.compress(stream.bson.dumps(_fixture("antares_locus.json")))
    return stream._parse_message(_KafkaMessage(raw))


def _antares_alerts(*_args, **_kwargs):
    """Stand-in for the client's REST fetch of a locus's alerts (no network)."""
    from antares_client._api.schemas import _AlertSchema

    return iter(_AlertSchema(many=True).load(_fixture("antares_alerts.json")))


@pytest.mark.django_db
def test_antares_locus_decodes_normalizes_and_ingests():
    locus = _antares_locus()

    with mock.patch(
        "antares_client.models._list_resources", side_effect=_antares_alerts
    ) as fetch:
        raw = locus.alerts[0].properties

    fetch.assert_called_once()
    canonical = normalize_antares(raw)
    assert canonical["lsst_diaObject_diaObjectId"] == 170433143923278190
    assert canonical["ra_deg"] == 238.986105
    assert ingest_alert(canonical, "antares") is True
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=170433143923278190)
    assert alert.lsst_diaSource_diaSourceId == 170433143923278191


def test_antares_lightcurve_parses_under_pandas3():
    lightcurve = _antares_locus().lightcurve

    assert list(lightcurve.columns)[:3] == ["alert_id", "ant_mjd", "ant_survey"]
    assert len(lightcurve) == 2
    assert lightcurve["ant_passband"].tolist() == ["g", "r"]
    # pandas 3 reads strings as its string dtype, not object.
    assert lightcurve["ant_passband"].dtype != object
    assert lightcurve["ant_mag"].isna().tolist() == [False, True]


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
