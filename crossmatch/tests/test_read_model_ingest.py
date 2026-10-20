"""U3 / R2, R3, R4, R5: per-broker reliability extraction at normalize time, and
first-seen reliability + computed healpix_ipix at ingest.

- AE1: reliability read from the broker-specific path; null when the key is absent.
- AE2: first delivery's reliability is frozen; later deliveries do not update it.
- AE3 (storage half): a payload lacking the key persists reliability=None.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pandas as pd
import pytest
from django.utils import timezone

from brokers import ingest_alert
from brokers.normalize import (
    antares_locus_first_detection_mjd,
    normalize_antares,
    normalize_lasair,
    normalize_pittgoogle,
)
from core.healpix import radec_to_ipix
from core.models import Alert


class _PittGoogleAlert:
    """Minimal stand-in for the Pitt-Google client alert (exposes .dict)."""

    def __init__(self, payload):
        self.dict = payload


def _antares_raw(reliability=0.8):
    raw = {
        "lsst_diaObject_diaObjectId": 9_200_000_001,
        "lsst_diaObject_ra": 180.0,
        "lsst_diaObject_dec": -30.0,
        "lsst_diaSource_diaSourceId": 9_200_000_002,
        "ant_time_received": 1_700_000_000.0,
    }
    if reliability is not None:
        raw["lsst_diaSource_reliability"] = reliability
    return raw


def _lasair_raw(reliability=0.75):
    raw = {
        "diaObjectId": 9_300_000_001,
        "ra": 12.0,
        "decl": -5.0,
        "firstDiaSourceMjdTai": 60000.0,
    }
    if reliability is not None:
        raw["latestR"] = reliability
    return raw


def _pittgoogle_payload(reliability=0.9):
    dia_source = {"diaSourceId": 9_400_000_002, "midpointMjdTai": 60000.0}
    if reliability is not None:
        dia_source["reliability"] = reliability
    return {
        "diaObject": {"diaObjectId": 9_400_000_001, "ra": 200.0, "dec": 10.0},
        "diaSource": dia_source,
    }


# ── AE1: per-broker extraction path map ──────────────────────────────────────

def test_antares_reliability_from_flat_key():
    assert normalize_antares(_antares_raw(0.83))["reliability"] == 0.83


def test_lasair_reliability_from_latestr():
    assert normalize_lasair(_lasair_raw(0.71))["reliability"] == 0.71


def test_pittgoogle_reliability_from_nested_key():
    alert = _PittGoogleAlert(_pittgoogle_payload(0.92))
    assert normalize_pittgoogle(alert)["reliability"] == 0.92


def test_missing_reliability_key_yields_none():
    assert normalize_antares(_antares_raw(None))["reliability"] is None
    assert normalize_lasair(_lasair_raw(None))["reliability"] is None
    assert normalize_pittgoogle(_PittGoogleAlert(_pittgoogle_payload(None)))["reliability"] is None


# ── AE2: first-seen reliability freeze + ipix at ingest ──────────────────────

def _canonical(dia_id=9_500_000_001, reliability=None, ra_deg=100.0, dec_deg=-45.0):
    return {
        "lsst_diaObject_diaObjectId": dia_id,
        "ra_deg": ra_deg,
        "dec_deg": dec_deg,
        "lsst_diaSource_diaSourceId": dia_id + 1,
        "event_time": timezone.now(),
        "reliability": reliability,
        "payload": {"x": 1},
    }


@pytest.mark.django_db
def test_reliability_is_frozen_first_seen():
    # Covers AE2.
    ingest_alert(_canonical(reliability=0.70), "antares")
    ingest_alert(_canonical(reliability=0.90), "lasair")
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=9_500_000_001)
    assert alert.reliability == 0.70


@pytest.mark.django_db
def test_ipix_computed_at_ingest_and_frozen_first_seen():
    ingest_alert(_canonical(reliability=0.70, ra_deg=100.0, dec_deg=-45.0), "antares")
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=9_500_000_001)
    expected = radec_to_ipix(100.0, -45.0)
    assert alert.healpix_ipix == expected
    # A repeat delivery reporting DIFFERENT coordinates does not recompute or
    # change ipix — it stays frozen at the first delivery's position.
    ingest_alert(_canonical(reliability=0.90, ra_deg=250.0, dec_deg=10.0), "lasair")
    alert.refresh_from_db()
    assert alert.healpix_ipix == expected
    assert alert.healpix_ipix != radec_to_ipix(250.0, 10.0)


@pytest.mark.django_db
def test_null_reliability_persists_as_none():
    # Covers AE3 (storage half): a payload with no reliability stores NULL.
    ingest_alert(_canonical(reliability=None), "lasair")
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=9_500_000_001)
    assert alert.reliability is None
    assert alert.healpix_ipix is not None


@pytest.mark.django_db
def test_out_of_range_coordinate_stores_null_ipix_without_dropping_alert():
    # A declination beyond the pole must not abort ingest (cdshealpix would
    # raise/panic); healpix_ipix degrades to NULL and the alert still persists.
    assert (
        ingest_alert(
            _canonical(dia_id=9_600_000_001, ra_deg=10.0, dec_deg=95.0), "antares"
        )
        is True
    )
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=9_600_000_001)
    assert alert.healpix_ipix is None


# ── R20 / KTD9: event time and its observation-time basis ────────────────────

_EPOCH = datetime(1858, 11, 17, tzinfo=dt_timezone.utc)


def _mjd(days):
    return _EPOCH + timedelta(days=days)


class _Locus:
    """Minimal stand-in for an antares-client Locus (properties + lightcurve)."""

    def __init__(self, properties=None, lightcurve=None):
        self.properties = properties or {}
        self.lightcurve = lightcurve


def _lightcurve(rows):
    return pd.DataFrame(rows, columns=["alert_id", "ant_mjd", "ant_mag"])


def test_lasair_event_time_is_first_detection():
    canonical = normalize_lasair(_lasair_raw())
    assert canonical["event_time"] == _mjd(60000.0)
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.FIRST_DETECTION


def test_pittgoogle_object_first_detection_wins():
    payload = _pittgoogle_payload()
    payload["diaObject"]["firstDiaSourceMjdTai"] = 59990.0
    payload["prvDiaSources"] = [{"midpointMjdTai": 59995.0}]
    canonical = normalize_pittgoogle(_PittGoogleAlert(payload))
    assert canonical["event_time"] == _mjd(59990.0)
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.FIRST_DETECTION


def test_pittgoogle_history_gives_earliest_midpoint_as_first_detection():
    # Covers AE10 (second object): history shows an earlier detection.
    payload = _pittgoogle_payload()
    payload["prvDiaSources"] = [
        {"midpointMjdTai": 59998.5},
        {"midpointMjdTai": 59997.25},
    ]
    canonical = normalize_pittgoogle(_PittGoogleAlert(payload))
    assert canonical["event_time"] == _mjd(59997.25)
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.FIRST_DETECTION


def test_pittgoogle_without_history_uses_diasource_as_later_detection():
    # Covers AE10 (first object): no history, triggering detection only.
    for history in (None, []):
        payload = _pittgoogle_payload()
        payload["prvDiaSources"] = history
        canonical = normalize_pittgoogle(_PittGoogleAlert(payload))
        assert canonical["event_time"] == _mjd(60000.0)
        assert canonical["event_time_basis"] == Alert.EventTimeBasis.LATER_DETECTION


def test_pittgoogle_without_midpoint_or_history_uses_ingest_time():
    payload = _pittgoogle_payload()
    del payload["diaSource"]["midpointMjdTai"]
    before = timezone.now()
    canonical = normalize_pittgoogle(_PittGoogleAlert(payload))
    assert canonical["event_time"] >= before
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.INGEST_TIME


def test_pittgoogle_malformed_history_falls_back_without_raising():
    for history in (["junk", {"midpointMjdTai": "x"}, {"other": 1}, None], "junk", {"a": 1}):
        payload = _pittgoogle_payload()
        payload["prvDiaSources"] = history
        canonical = normalize_pittgoogle(_PittGoogleAlert(payload))
        assert canonical["event_time"] == _mjd(60000.0)
        assert canonical["event_time_basis"] == Alert.EventTimeBasis.LATER_DETECTION


def test_antares_locus_first_detection_from_oldest_observation_time():
    locus = _Locus({"oldest_alert_id": "lsst:1", "oldest_alert_observation_time": 59950.5})
    mjd = antares_locus_first_detection_mjd(locus)
    assert mjd == 59950.5
    canonical = normalize_antares(_antares_raw(), locus_first_detection_mjd=mjd)
    assert canonical["event_time"] == _mjd(59950.5)
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.FIRST_DETECTION


def test_antares_locus_first_detection_from_lightcurve_detections():
    locus = _Locus(lightcurve=_lightcurve([
        ("lsst:2", 59961.0, 21.5),
        ("lsst:1", 59960.0, 22.0),
    ]))
    assert antares_locus_first_detection_mjd(locus) == 59960.0


def test_antares_lightcurve_skips_earlier_upper_limit_and_forced_rows():
    locus = _Locus(lightcurve=_lightcurve([
        ("lsst:0", 59950.0, None),
        ("ztf_upper_limit:5", 59951.0, 19.0),
        ("lsst_forced_source:7", 59952.0, 23.0),
        ("lsst:1", 59960.0, 22.0),
    ]))
    assert antares_locus_first_detection_mjd(locus) == 59960.0


def test_antares_ignores_other_surveys_history():
    # A locus can also hold ZTF history from years before Rubin; it must never
    # place a Rubin object.
    locus = _Locus(
        {"oldest_alert_id": "ztf_candidate:9", "oldest_alert_observation_time": 58000.0},
        lightcurve=_lightcurve([
            ("ztf_candidate:9", 58000.0, 18.0),
            ("lsst:1", 59960.0, 22.0),
        ]),
    )
    assert antares_locus_first_detection_mjd(locus) == 59960.0


def test_antares_lightcurve_without_detections_falls_through_to_alert_midpoint():
    locus = _Locus(lightcurve=_lightcurve([("lsst:0", 59950.0, None)]))
    mjd = antares_locus_first_detection_mjd(locus)
    assert mjd is None
    raw = _antares_raw()
    raw["lsst_diaSource_midpointMjdTai"] = 59970.0
    canonical = normalize_antares(raw, locus_first_detection_mjd=mjd)
    assert canonical["event_time"] == _mjd(59970.0)
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.LATER_DETECTION


def test_antares_locus_extraction_never_raises():
    class _Broken:
        @property
        def properties(self):
            raise RuntimeError("boom")

        @property
        def lightcurve(self):
            raise RuntimeError("boom")

    assert antares_locus_first_detection_mjd(_Broken()) is None
    assert antares_locus_first_detection_mjd(_Locus(lightcurve=pd.DataFrame())) is None


def test_antares_alert_midpoint_then_receive_time():
    raw = _antares_raw()
    raw["ant_mjd"] = 59971.0
    canonical = normalize_antares(raw)
    assert canonical["event_time"] == _mjd(59971.0)
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.LATER_DETECTION

    canonical = normalize_antares(_antares_raw())
    assert canonical["event_time"] == datetime.fromtimestamp(1_700_000_000.0, tz=dt_timezone.utc)
    assert canonical["event_time_basis"] == Alert.EventTimeBasis.BROKER_RECEIVE


@pytest.mark.django_db
def test_event_time_and_basis_are_frozen_first_seen():
    first = _canonical()
    first["event_time"] = _mjd(60000.0)
    first["event_time_basis"] = Alert.EventTimeBasis.LATER_DETECTION
    ingest_alert(first, "pittgoogle")
    second = _canonical()
    second["event_time"] = _mjd(59990.0)
    second["event_time_basis"] = Alert.EventTimeBasis.FIRST_DETECTION
    assert ingest_alert(second, "lasair") is True
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=9_500_000_001)
    assert alert.event_time == _mjd(60000.0)
    assert alert.event_time_basis == Alert.EventTimeBasis.LATER_DETECTION


@pytest.mark.django_db
def test_canonical_without_basis_stores_null():
    ingest_alert(_canonical(), "lasair")
    alert = Alert.objects.get(lsst_diaObject_diaObjectId=9_500_000_001)
    assert alert.event_time_basis is None
