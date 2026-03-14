---
title: "Auto-recover stuck QUEUED alerts"
type: feat
status: completed
date: 2026-03-13
origin: docs/brainstorms/2026-03-13-recover-stuck-queued-alerts-brainstorm.md
---

# Auto-Recover Stuck QUEUED Alerts

## Overview

Add time-based auto-recovery to the `dispatch_crossmatch_batch` concurrency
guard so that alerts stuck in QUEUED status are automatically reverted to
INGESTED after a timeout, unblocking the pipeline without manual
intervention.

## Proposed Solution

Modify the concurrency guard in `dispatch_crossmatch_batch` (`tasks/schedule.py:29`)
to check the age of the oldest QUEUED alert before deciding to skip. If the
age exceeds `settings.CELERY_TASK_TIME_LIMIT * 2` (~2 hours), revert all
QUEUED alerts to INGESTED and log a structured warning.

### 1. Update the concurrency guard in `tasks/schedule.py`

The current guard at line 29:

```python
if Alert.objects.filter(status=Alert.Status.QUEUED).exists():
    return
```

Replace with:

```python
# tasks/schedule.py — inside dispatch_crossmatch_batch()
queued = Alert.objects.filter(status=Alert.Status.QUEUED)
if queued.exists():
    oldest_queued = queued.order_by('ingest_time').first()
    age = (timezone.now() - oldest_queued.ingest_time).total_seconds()
    stuck_threshold = settings.CELERY_TASK_TIME_LIMIT * 2
    if age < stuck_threshold:
        return  # Batch legitimately in progress
    # Alerts are stuck — revert to INGESTED
    count = queued.update(status=Alert.Status.INGESTED)
    logger.warning('Auto-recovered stuck QUEUED alerts',
                   count=count, oldest_age_seconds=age,
                   threshold_seconds=stuck_threshold)
    # Fall through to normal threshold checks below
```

Key points:
- Uses `ingest_time` as a conservative proxy for QUEUED age, since the Alert
  model has no `updated_at` or `queued_at` field. `ingest_time` is always
  older than the actual queued time, so recovery triggers sooner, never later
  (see brainstorm decision #1).
- Threshold is computed from `settings.CELERY_TASK_TIME_LIMIT * 2`, not
  hardcoded (see brainstorm decision #1).
- After recovery, falls through to the normal INGESTED threshold checks so
  the recovered alerts can be re-dispatched in the same tick.

File: `crossmatch/tasks/schedule.py:29-31`

## Acceptance Criteria

- [x] Concurrency guard checks QUEUED alert age before skipping
- [x] Threshold is `settings.CELERY_TASK_TIME_LIMIT * 2` (dynamic, not hardcoded)
- [x] Stuck QUEUED alerts are reverted to INGESTED when threshold exceeded
- [x] Structured warning logged with count, age, and threshold
- [x] Normal batch dispatch resumes after recovery (fall-through)
- [x] Existing behavior preserved: QUEUED alerts younger than threshold still block new batches

## References

- **Origin brainstorm:** `docs/brainstorms/2026-03-13-recover-stuck-queued-alerts-brainstorm.md`
  - Key decisions: time-based recovery over startup revert (K8s safe),
    2x task time limit threshold, `ingest_time` as proxy, log-only
    observability, no retry limit
- Concurrency guard: `crossmatch/tasks/schedule.py:29`
- Existing failure revert: `crossmatch/tasks/crossmatch.py:86-95`
- Task time limit setting: `crossmatch/project/settings.py:119`
- Alert model (no `updated_at`): `crossmatch/core/models.py:8-52`
