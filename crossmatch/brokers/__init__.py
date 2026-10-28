"""Shared alert ingest helper used by all broker consumers."""

from django.db import close_old_connections, connection

from core.models import Alert, AlertDelivery
from core.healpix import radec_to_ipix
from core.log import get_logger
from core.metrics import record_ingest_success

logger = get_logger(__name__)


def ingest_alert(canonical: dict, broker: str) -> bool:
    """Two-step atomic ingest gate (§5.3).

    Step 1: upsert the Alert row by lsst_diaObject_diaObjectId.
    Step 2: record delivery for this broker; if already recorded, skip.

    Alerts remain at status=INGESTED for the Celery Beat batch dispatcher
    to pick up. Returns True on first delivery, False if already delivered.

    canonical keys:
        lsst_diaObject_diaObjectId, ra_deg, dec_deg,
        lsst_diaSource_diaSourceId, event_time, event_time_basis, reliability,
        payload (event_time_basis may be absent, which stores NULL)

    Read-model columns are populated first-seen: ``reliability`` (the broker's
    real/bogus score, or None when absent) and ``healpix_ipix`` (derived from
    the coordinates) are written only when the Alert row is first created, via
    the ``get_or_create`` defaults. ``event_time`` and ``event_time_basis`` are
    first-seen the same way. Repeat deliveries of the same object leave all of
    them unchanged.
    """
    # The broker consumers call this from long-lived management-command loops
    # with no request/response cycle, so Django never fires the signals that
    # normally recycle DB connections. Recycle per work unit (as Django does per
    # request and Celery does per task): drop a stale/dead connection here so a
    # server-side disconnect can't wedge every later query with psycopg's
    # "the connection is closed" until the pod restarts. With CONN_HEALTH_CHECKS
    # and CONN_MAX_AGE set, a healthy connection is validated and reused rather
    # than reopened, keeping the ingest hot path cheap. Skip while a transaction
    # is open on the connection -- closing it mid-transaction would abort it; the
    # consumers call this outside any atomic block.
    if not connection.in_atomic_block:
        close_old_connections()
    alert_id = canonical['lsst_diaObject_diaObjectId']
    try:
        # Fast path for repeat deliveries (the common case under multi-broker
        # fan-in): the row already exists, so avoid building defaults and paying
        # the discarded HEALPix computation on the ingest hot path. get_or_create
        # below still handles the first-seen create race.
        alert_obj = Alert.objects.get(lsst_diaObject_diaObjectId=alert_id)
    except Alert.DoesNotExist:
        alert_obj, _ = Alert.objects.get_or_create(
            lsst_diaObject_diaObjectId=alert_id,
            defaults=dict(
                ra_deg=canonical['ra_deg'],
                dec_deg=canonical['dec_deg'],
                lsst_diaSource_diaSourceId=canonical.get('lsst_diaSource_diaSourceId'),
                event_time=canonical['event_time'],
                event_time_basis=canonical.get('event_time_basis'),
                reliability=canonical.get('reliability'),
                healpix_ipix=radec_to_ipix(canonical['ra_deg'], canonical['dec_deg']),
                payload=canonical['payload'],
                status=Alert.Status.INGESTED,
            ),
        )
    _, created = AlertDelivery.objects.get_or_create(
        alert=alert_obj,
        broker=broker,
    )
    if not created:
        logger.info(
            'alert already delivered by this broker, skipping',
            alert_id=alert_id,
            broker=broker,
        )
        record_ingest_success(broker, 'duplicate')
        return False
    logger.info(f'New alert ingested: {alert_obj}')
    record_ingest_success(broker, 'new')
    return True
