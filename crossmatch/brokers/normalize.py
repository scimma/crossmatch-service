"""Alert normalization — maps broker-specific alert schemas to the internal canonical format."""

import math
from datetime import datetime, timedelta, timezone

from core.log import get_logger
from core.models import Alert

logger = get_logger(__name__)

# MJD epoch in UTC: 1858-11-17 00:00:00 UTC.
# TAI-UTC offset (~37 s) is negligible for alert event-time purposes.
_MJD_EPOCH = datetime(1858, 11, 17, tzinfo=timezone.utc)

# ANTARES lightcurve rows that are not detections carry one of these markers in
# their alert_id (e.g. ztf_upper_limit:..., lsst_forced_source:...).
_ANTARES_NON_DETECTION_MARKERS = ('upper_limit', 'forced')
_ANTARES_RUBIN_ALERT_PREFIX = 'lsst:'

Basis = Alert.EventTimeBasis


def _mjd(value) -> float | None:
    """Return ``value`` as a finite MJD float, or None if it is not one.

    Args:
        value: A candidate MJD from a broker payload, of any type.

    Returns:
        The MJD as a float, or None for missing, non-numeric, or non-finite
        values (bools are rejected).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _mjd_to_datetime(mjd: float) -> datetime:
    """Convert an MJD to a UTC datetime via the _MJD_EPOCH pattern."""
    return _MJD_EPOCH + timedelta(days=mjd)


def antares_locus_first_detection_mjd(locus) -> float | None:
    """Return the locus-level first-detection MJD of an ANTARES locus.

    Prefers the locus property ``oldest_alert_observation_time`` when the
    locus's ``oldest_alert_id`` is a Rubin (``lsst:``) alert; otherwise the
    minimum ``ant_mjd`` over Rubin lightcurve rows that are detections (a
    non-null ``ant_mag`` and an ``alert_id`` that is not an upper-limit or
    forced-photometry row). A locus can also hold other surveys' history (e.g.
    ZTF), which must never place a Rubin object. Never raises: anything
    malformed yields None so the normalizer falls through to the alert's own
    time.

    Args:
        locus: An antares-client Locus (or any object with ``properties`` and
            ``lightcurve``).

    Returns:
        The first-detection MJD, or None when the locus offers none.
    """
    try:
        properties = locus.properties or {}
        oldest_id = str(properties.get('oldest_alert_id') or '').lower()
        if oldest_id.startswith(_ANTARES_RUBIN_ALERT_PREFIX):
            oldest = _mjd(properties.get('oldest_alert_observation_time'))
            if oldest is not None:
                return oldest
    except Exception as err:
        logger.warning('ANTARES locus properties unreadable', error=str(err))
    try:
        lightcurve = locus.lightcurve
        if (
            lightcurve is None
            or 'ant_mjd' not in lightcurve
            or 'ant_mag' not in lightcurve
        ):
            return None
        if 'alert_id' not in lightcurve:
            return None
        rows = lightcurve[lightcurve['ant_mag'].notna()]
        alert_ids = rows['alert_id'].astype(str).str.lower()
        rubin = alert_ids.str.startswith(_ANTARES_RUBIN_ALERT_PREFIX)
        non_detection = alert_ids.str.contains('|'.join(_ANTARES_NON_DETECTION_MARKERS))
        rows = rows[rubin & ~non_detection]
        mjds = [m for m in (_mjd(v) for v in rows['ant_mjd'].tolist()) if m is not None]
        return min(mjds) if mjds else None
    except Exception as err:
        logger.warning('ANTARES locus lightcurve unreadable', error=str(err))
        return None


def select_antares_rubin_alert(alerts: list) -> object | None:
    """Return the newest Rubin alert on an ANTARES locus, or None.

    ANTARES lists a locus's alerts oldest-first, and a locus can hold years
    of other surveys' history (e.g. ZTF) at either end, so neither the first
    nor the last alert is reliably the Rubin one. Among alerts whose
    properties carry ``lsst_diaObject_diaObjectId``, pick the greatest
    ``mjd``; on a tie, the one later in the API's order.

    Args:
        alerts: The locus's antares-client Alert models, in API order.

    Returns:
        The chosen Alert, or None when no alert is a Rubin alert.
    """
    chosen = None
    for alert in alerts:
        if 'lsst_diaObject_diaObjectId' not in (alert.properties or {}):
            continue
        if chosen is None or alert.mjd >= chosen.mjd:
            chosen = alert
    return chosen


def normalize_antares(
    raw_alert: dict, locus_first_detection_mjd: float | None = None
) -> dict:
    """Normalize an ANTARES alert to the internal canonical format.

    ANTARES alerts carry LSST-native field names prefixed with lsst_diaObject_
    and lsst_diaSource_, plus ant_* ANTARES annotations.

    event_time is the locus first-detection time when the consumer supplies
    one (basis first_detection), else the alert's own midpoint (later_detection),
    else ant_time_received (broker_receive).

    Args:
        raw_alert: The alert's properties dict.
        locus_first_detection_mjd: The locus-level first-detection MJD, from
            antares_locus_first_detection_mjd(), or None.

    Returns:
        The canonical alert dict, including event_time and event_time_basis.
    """
    first_mjd = _mjd(locus_first_detection_mjd)
    own_mjd = _mjd(raw_alert.get('lsst_diaSource_midpointMjdTai'))
    if own_mjd is None:
        own_mjd = _mjd(raw_alert.get('ant_mjd'))
    if first_mjd is not None:
        event_time, basis = _mjd_to_datetime(first_mjd), Basis.FIRST_DETECTION
    elif own_mjd is not None:
        event_time, basis = _mjd_to_datetime(own_mjd), Basis.LATER_DETECTION
    else:
        event_time = datetime.fromtimestamp(
            raw_alert['ant_time_received'], tz=timezone.utc
        )
        basis = Basis.BROKER_RECEIVE
    return {
        'lsst_diaObject_diaObjectId': raw_alert['lsst_diaObject_diaObjectId'],
        'ra_deg': raw_alert['lsst_diaObject_ra'],
        'dec_deg': raw_alert['lsst_diaObject_dec'],
        'lsst_diaSource_diaSourceId': raw_alert['lsst_diaSource_diaSourceId'],
        'event_time': event_time,
        'event_time_basis': basis,
        'reliability': raw_alert.get('lsst_diaSource_reliability'),
        'payload': raw_alert,
    }


def normalize_lasair(raw_alert: dict) -> dict:
    """Normalize a Lasair alert to the internal canonical format.

    Lasair Kafka messages contain the filter SQL columns:
    diaObjectId, firstDiaSourceMjdTai, ra, decl.
    No diaSource ID is provided.

    event_time is derived from firstDiaSourceMjdTai (MJD-TAI) converted to UTC.
    """
    return {
        'lsst_diaObject_diaObjectId': raw_alert['diaObjectId'],
        'ra_deg': raw_alert['ra'],
        'dec_deg': raw_alert['decl'],
        'lsst_diaSource_diaSourceId': None,
        'event_time': _MJD_EPOCH + timedelta(days=raw_alert['firstDiaSourceMjdTai']),
        'event_time_basis': Basis.FIRST_DETECTION,
        'reliability': raw_alert.get('latestR'),
        'payload': raw_alert,
    }


def _source_mjd(source: dict) -> float | None:
    """A diaSource's midpoint MJD-TAI (either schema spelling), or None."""
    return _mjd(source.get('midpointMjdTai') or source.get('midPointTai'))


def normalize_pittgoogle(alert) -> dict:
    """Normalize a Pitt-Google alert to the internal canonical format.

    Subscribes to the lsst-alerts-json topic. alert.dict returns the parsed
    JSON payload, which preserves the LSST Avro schema's nested structure
    (diaObject, diaSource). The .objectid/.sourceid/.ra/.dec convenience
    accessors are not used because they require schema_name='lsst' (Avro
    Confluent-wire deserialization), which doesn't apply to the JSON topic.

    event_time is the best-available first-detection time, converted with the
    same _MJD_EPOCH pattern as normalize_lasair(): diaObject.firstDiaSourceMjdTai
    (first_detection), else the earliest midpoint over prvDiaSources and the
    diaSource when the history holds any usable entry (first_detection), else
    the diaSource midpoint (later_detection), else now (ingest_time). Malformed
    history falls through to the next source rather than raising.
    """
    payload = alert.dict
    dia_object = payload['diaObject']
    dia_source = payload['diaSource']

    source_mjd = _source_mjd(dia_source)
    history_mjds = []
    history = payload.get('prvDiaSources')
    if isinstance(history, list):
        for entry in history:
            if isinstance(entry, dict):
                mjd = _source_mjd(entry)
                if mjd is not None:
                    history_mjds.append(mjd)

    object_first_mjd = _mjd(dia_object.get('firstDiaSourceMjdTai'))
    if object_first_mjd is not None:
        event_time, basis = _mjd_to_datetime(object_first_mjd), Basis.FIRST_DETECTION
    elif history_mjds:
        earliest = min(history_mjds + ([source_mjd] if source_mjd is not None else []))
        event_time, basis = _mjd_to_datetime(earliest), Basis.FIRST_DETECTION
    elif source_mjd is not None:
        event_time, basis = _mjd_to_datetime(source_mjd), Basis.LATER_DETECTION
    else:
        event_time, basis = datetime.now(tz=timezone.utc), Basis.INGEST_TIME

    return {
        'lsst_diaObject_diaObjectId': dia_object['diaObjectId'],
        'ra_deg': dia_object['ra'],
        'dec_deg': dia_object['dec'],
        'lsst_diaSource_diaSourceId': dia_source.get('diaSourceId'),
        'event_time': event_time,
        'event_time_basis': basis,
        'reliability': dia_source.get('reliability'),
        'payload': payload,
    }
