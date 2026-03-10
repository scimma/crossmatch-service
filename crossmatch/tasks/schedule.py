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
    1. Concurrency guard: if any QUEUED alerts exist, skip.
    2. Count threshold: INGESTED count >= CROSSMATCH_BATCH_MAX_SIZE.
    3. Time threshold: oldest INGESTED alert age >= CROSSMATCH_BATCH_MAX_WAIT_SECONDS.
    """
    from django.utils import timezone
    from django.db import transaction
    from core.models import Alert
    from tasks.crossmatch import crossmatch_batch

    # Concurrency guard: skip if a batch is already in progress
    if Alert.objects.filter(status=Alert.Status.QUEUED).exists():
        return

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


periodic_tasks = [
    DispatchCrossmatchBatch(),
]
