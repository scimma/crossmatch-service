"""Alert normalization — maps broker-specific alert schemas to the internal canonical format."""

from datetime import datetime, timedelta, timezone

# MJD epoch in UTC: 1858-11-17 00:00:00 UTC.
# TAI-UTC offset (~37 s) is negligible for alert event-time purposes.
_MJD_EPOCH = datetime(1858, 11, 17, tzinfo=timezone.utc)


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


def normalize_antares(raw_alert: dict) -> dict:
    """Normalize an ANTARES alert to the internal canonical format.

    ANTARES alerts carry LSST-native field names prefixed with lsst_diaObject_
    and lsst_diaSource_, plus ant_* ANTARES annotations.
    """
    return {
        'lsst_diaObject_diaObjectId': raw_alert['lsst_diaObject_diaObjectId'],
        'ra_deg': raw_alert['lsst_diaObject_ra'],
        'dec_deg': raw_alert['lsst_diaObject_dec'],
        'lsst_diaSource_diaSourceId': raw_alert['lsst_diaSource_diaSourceId'],
        'event_time': datetime.fromtimestamp(raw_alert['ant_time_received'], tz=timezone.utc),
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
        'reliability': raw_alert.get('latestR'),
        'payload': raw_alert,
    }


def normalize_pittgoogle(alert) -> dict:
    """Normalize a Pitt-Google alert to the internal canonical format.

    Subscribes to the lsst-alerts-json topic. alert.dict returns the parsed
    JSON payload, which preserves the LSST Avro schema's nested structure
    (diaObject, diaSource). The .objectid/.sourceid/.ra/.dec convenience
    accessors are not used because they require schema_name='lsst' (Avro
    Confluent-wire deserialization), which doesn't apply to the JSON topic.

    event_time is derived from the LSST alert's MJD-TAI timestamp, converted
    to a datetime using the same _MJD_EPOCH pattern as normalize_lasair().
    """
    payload = alert.dict
    dia_object = payload['diaObject']
    dia_source = payload['diaSource']

    mjd_tai = dia_source.get('midpointMjdTai') or dia_source.get('midPointTai')
    if mjd_tai is not None:
        event_time = _MJD_EPOCH + timedelta(days=mjd_tai)
    else:
        event_time = datetime.now(tz=timezone.utc)

    return {
        'lsst_diaObject_diaObjectId': dia_object['diaObjectId'],
        'ra_deg': dia_object['ra'],
        'dec_deg': dia_object['dec'],
        'lsst_diaSource_diaSourceId': dia_source.get('diaSourceId'),
        'event_time': event_time,
        'reliability': dia_source.get('reliability'),
        'payload': payload,
    }
