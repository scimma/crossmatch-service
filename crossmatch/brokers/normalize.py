"""Alert normalization — maps broker-specific alert schemas to the internal canonical format."""

from datetime import datetime, timedelta, timezone


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
    diaObjectId, ra, decl, nDiaSources, latestR, age (days since last detection).
    No diaSource ID is provided.
    """
    return {
        'lsst_diaObject_diaObjectId': raw_alert['diaObjectId'],
        'ra_deg': raw_alert['ra'],
        'dec_deg': raw_alert['decl'],
        'lsst_diaSource_diaSourceId': None,
        'event_time': datetime.now(tz=timezone.utc) - timedelta(days=raw_alert['age']),
        'payload': raw_alert,
    }
