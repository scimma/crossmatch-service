"""Alert normalization — maps broker-specific alert schemas to the internal canonical format."""

from datetime import datetime, timedelta, timezone

# MJD epoch in UTC: 1858-11-17 00:00:00 UTC.
# TAI-UTC offset (~37 s) is negligible for alert event-time purposes.
_MJD_EPOCH = datetime(1858, 11, 17, tzinfo=timezone.utc)


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
        'payload': raw_alert,
    }
