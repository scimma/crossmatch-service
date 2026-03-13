from celery import shared_task
from django.conf import settings
from core.log import get_logger
logger = get_logger(__name__)


class DispatchCrossmatchBatch:
    task_name = 'Dispatch Crossmatch Batch'
    task_handle = 'dispatch_crossmatch_batch'
    task_frequency_seconds = 30
    task_initially_enabled = True


@shared_task
def dispatch_crossmatch_batch() -> None:
    """Check batch thresholds and dispatch a crossmatch batch if met.

    Runs every 30 seconds via Celery Beat. Checks:
    1. Concurrency guard: if any QUEUED alerts exist, skip (unless stuck).
    2. Count threshold: INGESTED count >= CROSSMATCH_BATCH_MAX_SIZE.
    3. Time threshold: oldest INGESTED alert age >= CROSSMATCH_BATCH_MAX_WAIT_SECONDS.
    """
    from django.utils import timezone
    from django.db import transaction
    from core.models import Alert
    from tasks.crossmatch import crossmatch_batch

    # Concurrency guard: skip if a batch is legitimately in progress.
    # If QUEUED alerts are older than 2x the task time limit, assume
    # the worker was killed and auto-recover by reverting to INGESTED.
    queued = Alert.objects.filter(status=Alert.Status.QUEUED)
    if queued.exists():
        oldest_queued = queued.order_by('ingest_time').first()
        age = (timezone.now() - oldest_queued.ingest_time).total_seconds()
        stuck_threshold = settings.CELERY_TASK_TIME_LIMIT * 2
        if age < stuck_threshold:
            return  # Batch legitimately in progress
        count_recovered = queued.update(status=Alert.Status.INGESTED)
        logger.warning('Auto-recovered stuck QUEUED alerts',
                       count=count_recovered, oldest_age_seconds=age,
                       threshold_seconds=stuck_threshold)

    # Check thresholds
    ingested = Alert.objects.filter(status=Alert.Status.INGESTED)
    count = ingested.count()
    if count == 0:
        return

    oldest = ingested.order_by('ingest_time').first()
    age = (timezone.now() - oldest.ingest_time).total_seconds()

    if (
        count < settings.CROSSMATCH_BATCH_MAX_SIZE
        and age < settings.CROSSMATCH_BATCH_MAX_WAIT_SECONDS
    ):
        return  # Neither threshold met

    # Dispatch batch: select IDs with row locking, transition, enqueue
    with transaction.atomic():
        batch_ids = list(
            ingested.order_by('ingest_time')
            .select_for_update(skip_locked=True)
            .values_list('pk', flat=True)
            [:settings.CROSSMATCH_BATCH_MAX_SIZE]
        )
        if not batch_ids:
            return
        Alert.objects.filter(pk__in=batch_ids).update(
            status=Alert.Status.QUEUED
        )
        # Convert UUIDs to strings for JSON serialization in Celery
        str_ids = [str(uid) for uid in batch_ids]
        transaction.on_commit(lambda: crossmatch_batch.delay(str_ids))

    logger.info('Dispatched crossmatch batch',
                batch_size=len(batch_ids), oldest_age_seconds=age)


class DispatchNotifications:
    task_name = 'Dispatch Notifications'
    task_handle = 'dispatch_notifications'
    task_frequency_seconds = 10
    task_initially_enabled = True


@shared_task
def dispatch_notifications() -> None:
    """Poll for pending notifications and dispatch to destination backends.

    Holds select_for_update lock during publishing to prevent concurrent
    Beat ticks from picking up the same rows.
    """
    from django.db import transaction
    from core.models import Alert, Notification
    from notifier.dispatch import DESTINATION_HANDLERS

    with transaction.atomic():
        pending = (
            Notification.objects.filter(state=Notification.State.PENDING)
            .select_for_update(skip_locked=True)
            .order_by('created_at')
            [:500]
        )
        pending_list = list(pending)

        if not pending_list:
            return

        # Group by destination and dispatch within the transaction
        by_dest = {}
        for notif in pending_list:
            by_dest.setdefault(notif.destination, []).append(notif)

        for destination, notifications in by_dest.items():
            handler = DESTINATION_HANDLERS.get(destination)
            if handler is None:
                logger.error('Unknown notification destination',
                             destination=destination)
                continue
            handler(notifications)

    # Check for alerts ready to transition to NOTIFIED
    alert_ids = {n.alert_id for n in pending_list
                 if n.state == Notification.State.SENT}
    for alert_id in alert_ids:
        has_unsent = Notification.objects.filter(
            alert_id=alert_id
        ).exclude(state=Notification.State.SENT).exists()
        if not has_unsent:
            Alert.objects.filter(
                pk=alert_id, status=Alert.Status.MATCHED
            ).update(status=Alert.Status.NOTIFIED)


periodic_tasks = [
    DispatchCrossmatchBatch(),
    DispatchNotifications(),
]
