---
title: "Batch crossmatch processing for LSDB performance"
type: feat
date: 2026-03-09
brainstorm: docs/brainstorms/2026-03-09-batch-crossmatch-brainstorm.md
reviewed_by: dhh-rails-reviewer, kieran-python-reviewer, code-simplicity-reviewer
---

# Batch crossmatch processing for LSDB performance

## Overview

Replace the per-alert `crossmatch_alert` Celery task with a batch model that
accumulates ingested alerts and processes them together. LSDB crossmatching
performs significantly better when alerts are processed as a batch — amortizing
HEALPix partition I/O across many alerts instead of opening the Gaia catalog
once per alert.

A Celery Beat periodic task checks every 30 seconds whether to dispatch a
batch, based on two tunable thresholds: maximum wall-clock wait time (default
15 min) and maximum batch size (default 100,000 alerts).

## Problem Statement / Motivation

The current design enqueues one `crossmatch_alert` Celery task per ingested
alert. With LSDB, each task would independently open the Gaia HATS catalog,
load overlapping partitions, and run a crossmatch — duplicating partition I/O
across thousands of alerts that cover the same sky region. Batching allows LSDB
to read each partition once and match all alerts in the batch against it.

## Proposed Solution

### Architecture

```
Broker → ingest_alert() → Alert(status=INGESTED)
                               ↓
Celery Beat (30s tick) → dispatch_crossmatch_batch()
    ├── Concurrency guard: QUEUED alerts exist? → skip
    ├── Count check: INGESTED >= max_size? → dispatch
    ├── Time check: oldest INGESTED age >= max_wait? → dispatch
    └── Neither met → skip
                               ↓
           transaction.atomic:
             SELECT ... FOR UPDATE SKIP LOCKED
             UPDATE ... SET status='QUEUED' LIMIT max_size
           transaction.on_commit → crossmatch_batch.delay()
                               ↓
Celery Worker → crossmatch_batch(match_version=1)
    ├── Capture alert_ids upfront
    ├── LSDB crossmatch (stub for now)
    ├── Success → status='MATCHED'
    └── Failure → status='INGESTED' (revert for retry)
```

## Technical Considerations

- **Atomicity**: The INGESTED→QUEUED status transition and the
  `crossmatch_batch.delay()` call must be coordinated. Use
  `transaction.atomic()` for the UPDATE and `transaction.on_commit()` to
  enqueue the Celery task only after the DB commit succeeds.
- **TOCTOU protection**: Use `select_for_update(skip_locked=True)` when
  selecting alert IDs for the batch. This prevents overlapping Beat ticks
  from selecting the same rows — a second tick skips already-locked rows.
- **Queryset pinning**: The batch task must capture `alert_ids` upfront
  (materialize the queryset into a list of IDs) and use
  `filter(id__in=alert_ids)` for all subsequent operations. Django querysets
  are lazy and re-evaluate on each use, which could cause status transitions
  to target the wrong rows if the set of QUEUED alerts changes.
- **Row locking**: A bulk UPDATE of up to 100k rows acquires row-level locks.
  This should not block concurrent ingests (they use `get_or_create` on
  different `diaObjectId` values) but could cause brief lock contention.
- **Poison alerts**: Deferred. The stub task cannot fail on specific alerts
  since it does no real crossmatching. When the real LSDB implementation lands,
  add per-alert failure tracking (retry counter or FAILED status).
- **CrossmatchRun**: Deferred. The existing model has a per-alert FK that
  doesn't fit batch operations. Will be revisited with the real LSDB
  implementation.

## Implementation Phases

### Phase 1: Remove per-alert dispatch from ingest

**File: `crossmatch/brokers/__init__.py`**

- Remove the import of `crossmatch_alert` (line 3)
- Remove the `crossmatch_alert.delay()` call (line 48)
- `ingest_alert()` still returns `True`/`False` for first-delivery detection
  (useful for logging) but no longer triggers a Celery task

### Phase 2: Add environment variables to settings

**File: `crossmatch/project/settings.py`**

Add two new settings:

```python
# Batch crossmatch thresholds
CROSSMATCH_BATCH_MAX_WAIT_SECONDS = int(
    os.getenv('CROSSMATCH_BATCH_MAX_WAIT_SECONDS', '900')
)
CROSSMATCH_BATCH_MAX_SIZE = int(
    os.getenv('CROSSMATCH_BATCH_MAX_SIZE', '100000')
)
```

The Beat tick interval (30s) is hardcoded in the periodic task descriptor —
it is an internal implementation detail, not an operator-tunable knob.

### Phase 3: Add dispatch periodic task

**File: `crossmatch/tasks/schedule.py`**

Add a `dispatch_crossmatch_batch` shared task and a
`DispatchCrossmatchBatch` descriptor class for registration via
`initialize_periodic_tasks`.

The dispatch task logic:

```python
@shared_task(name="dispatch_crossmatch_batch")
def dispatch_crossmatch_batch() -> None:
    from django.utils import timezone
    from django.db import transaction

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
            .values_list('id', flat=True)
            [:settings.CROSSMATCH_BATCH_MAX_SIZE]
        )
        if not batch_ids:
            return
        Alert.objects.filter(id__in=batch_ids).update(
            status=Alert.Status.QUEUED
        )
        transaction.on_commit(lambda: crossmatch_batch.delay())

    logger.info('Dispatched crossmatch batch',
                batch_size=len(batch_ids), oldest_age_seconds=age)
```

Add `DispatchCrossmatchBatch` to the `periodic_tasks` list:

```python
class DispatchCrossmatchBatch:
    task_name = 'Dispatch Crossmatch Batch'
    task_handle = 'dispatch_crossmatch_batch'
    task_frequency_seconds = 30
    task_initially_enabled = True
```

### Phase 4: Replace crossmatch task with batch task

**File: `crossmatch/tasks/crossmatch.py`**

Remove `crossmatch_alert` and the unused `json` import. Replace with
`crossmatch_batch`:

```python
@shared_task(name="crossmatch_batch")
def crossmatch_batch(match_version: int = 1) -> None:
    alert_ids = list(
        Alert.objects.filter(status=Alert.Status.QUEUED)
        .values_list('id', flat=True)
    )
    if not alert_ids:
        logger.info('No QUEUED alerts to process')
        return

    logger.info('Starting crossmatch batch',
                batch_size=len(alert_ids), match_version=match_version)
    try:
        # --- LSDB crossmatch stub ---
        for alert in Alert.objects.filter(id__in=alert_ids).iterator():
            logger.debug('Would crossmatch alert',
                         diaObjectId=alert.lsst_diaObject_diaObjectId)
        # --- end stub ---

        Alert.objects.filter(id__in=alert_ids).update(
            status=Alert.Status.MATCHED
        )
        logger.info('Crossmatch batch complete', batch_size=len(alert_ids))
    except Exception:
        logger.exception('Crossmatch batch failed, reverting to INGESTED',
                         batch_size=len(alert_ids))
        try:
            Alert.objects.filter(id__in=alert_ids).update(
                status=Alert.Status.INGESTED
            )
        except Exception:
            logger.exception('Failed to revert batch status')
        raise
```

Note: `CELERY_IMPORTS` already includes `"tasks.crossmatch"` and
`"tasks.schedule"`. No change needed — the new tasks are in the same modules.

### Phase 5: Update docker-compose environment

**File: `docker/docker-compose.yaml`**

Add the two environment variables to the `celery-worker` and `celery-beat`
services:

```yaml
CROSSMATCH_BATCH_MAX_WAIT_SECONDS: "${CROSSMATCH_BATCH_MAX_WAIT_SECONDS:-900}"
CROSSMATCH_BATCH_MAX_SIZE: "${CROSSMATCH_BATCH_MAX_SIZE:-100000}"
```

### Phase 6: Update Helm values

**File: `kubernetes/charts/crossmatch-service/values.yaml`**

Add batch configuration defaults under a new `crossmatch` section:

```yaml
crossmatch:
  batch_max_wait_seconds: 900
  batch_max_size: 100000
```

### Follow-up: Update design document

**File: `scimma_crossmatch_service_design.md`**

After the code is working, update the following sections:
- **§4.2 Ingest → Celery**: Replace per-alert task description with batch
  dispatch model
- **§6 Queue/Task Orchestration**: Add batch dispatch logic, thresholds, and
  concurrency guard
- **§8.4 Celery task definitions**: Replace `crossmatch_alert` with
  `dispatch_crossmatch_batch` and `crossmatch_batch`
- **§9 Configuration**: Add the two new environment variables

## Acceptance Criteria

- [x] `ingest_alert()` no longer calls `crossmatch_alert.delay()`
- [x] Alerts remain at `status=INGESTED` after ingest
- [x] `dispatch_crossmatch_batch` periodic task runs every 30s
- [x] Batch dispatched when INGESTED count >= `CROSSMATCH_BATCH_MAX_SIZE`
- [x] Batch dispatched when oldest INGESTED alert age >= `CROSSMATCH_BATCH_MAX_WAIT_SECONDS`
- [x] Batch capped at `CROSSMATCH_BATCH_MAX_SIZE` alerts
- [x] Status transitions: INGESTED → QUEUED (dispatch) → MATCHED (success)
- [x] On failure: QUEUED → INGESTED (revert for retry)
- [x] Concurrency guard: skip dispatch if QUEUED alerts exist
- [x] Batch task pins alert IDs upfront (no lazy queryset re-evaluation)
- [x] Dispatch uses `select_for_update(skip_locked=True)` for TOCTOU safety
- [x] `crossmatch_batch` task is a stub (logs alerts, no LSDB)
- [x] `crossmatch_alert` task is removed
- [x] Two env vars are configurable: max wait, max size
- [ ] Design document updated to reflect batch model

## Dependencies & Risks

- **django_celery_beat**: Already configured and working. The
  `initialize_periodic_tasks` management command handles registration.
- **Row locking on bulk UPDATE**: Low risk — ingests target different
  `diaObjectId` rows than the batch UPDATE selects. `skip_locked=True`
  prevents contention between overlapping dispatch ticks.

## Deferred Items

- Real LSDB crossmatch implementation (replaces the stub)
- Per-alert failure tracking / poison-alert quarantine
- `CrossmatchRun` model integration for batch audit trail
- Stale-batch safety valve: automatic recovery when a worker crashes and
  leaves alerts stuck in QUEUED. Requires either a `queued_at` timestamp
  field on the Alert model or external timestamp tracking. Not needed until
  the real LSDB implementation lands (the stub cannot crash).
- Prometheus metrics for batch size, duration, dispatch rate
- Notification pipeline integration (MATCHED → NOTIFIED)

## Review Feedback Incorporated

Changes made based on parallel review by DHH, Kieran, and Simplicity reviewers:

- **Removed stale-batch safety valve** (YAGNI) — stub task cannot crash in a
  way that orphans QUEUED alerts. Moved to Deferred Items.
- **Reduced env vars from 4 to 2** — removed `CROSSMATCH_BATCH_STALE_SECONDS`
  (valve removed) and `CROSSMATCH_BATCH_CHECK_INTERVAL` (hardcoded to 30s).
- **Added `select_for_update(skip_locked=True)`** — prevents TOCTOU race if
  two Beat ticks overlap.
- **Pinned alert IDs in batch task** — capture IDs upfront to avoid lazy
  queryset re-evaluation surprises.
- **Wrapped failure revert in nested try/except** — prevents DB errors during
  revert from masking the original exception.
- **Used `logger.exception`** instead of `logger.error` in failure paths to
  capture tracebacks.
- **Used parenthesized expressions** instead of backslash line continuation.
- **Used `.exists()`** for concurrency guard (cheaper than `.first()`).
- **Added return type annotations** (`-> None`) to all tasks.
- **Eliminated no-op Phase 5** (Celery imports unchanged).
- **Moved design doc update** to follow-up rather than implementation phase.
- **Noted `json` import cleanup** when removing `crossmatch_alert`.

## References

- Brainstorm: `docs/brainstorms/2026-03-09-batch-crossmatch-brainstorm.md`
- Existing periodic task pattern: `crossmatch/tasks/schedule.py`
- Alert model: `crossmatch/core/models.py:8-53`
- Current ingest dispatch: `crossmatch/brokers/__init__.py:48`
- Celery config: `crossmatch/project/settings.py:86-109`
