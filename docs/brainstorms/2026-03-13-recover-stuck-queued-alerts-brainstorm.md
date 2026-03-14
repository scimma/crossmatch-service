---
topic: Auto-recover stuck QUEUED alerts
date: 2026-03-13
status: complete
---

# Auto-Recover Stuck QUEUED Alerts

## Context

The `dispatch_crossmatch_batch` task has a concurrency guard: if any alerts
are in QUEUED status, it skips dispatching a new batch. This prevents
overlapping crossmatch runs. However, if a worker crashes or containers are
rebuilt while a `crossmatch_batch` task is in progress, the QUEUED alerts
never get reverted to INGESTED, and the pipeline stalls permanently.

The `crossmatch_batch` task does have a try/except that reverts QUEUED →
INGESTED on failure, but this only works when the exception handler actually
runs. A killed worker (OOM, container restart) bypasses Python exception
handling entirely.

## What We're Building

Time-based auto-recovery for stuck QUEUED alerts, integrated into the
existing `dispatch_crossmatch_batch` periodic task.

When the concurrency guard detects QUEUED alerts, instead of immediately
skipping, it checks the age of the oldest QUEUED alert (using `ingest_time`
as a conservative proxy). If that age exceeds a timeout threshold (2x the
Celery hard task time limit), the alerts are assumed orphaned and reverted
to INGESTED. A structured warning is logged.

## Why This Approach

### Chosen: Time-based auto-recovery in dispatch_crossmatch_batch

**Over startup revert:** Startup revert is simpler but has a race condition
in Kubernetes, where celery-beat can restart independently of workers. A
beat restart while a worker is mid-batch would incorrectly revert alerts
that are actively being processed. Time-based recovery is safe in all
deployment topologies.

**Over separate periodic task:** Adding another Beat task is more modular
but unnecessary complexity. The recovery check belongs naturally at the
point where the concurrency guard runs — it's the same concern.

**Over management command:** Manual intervention is easy to forget and
doesn't self-heal. The system should recover automatically.

## Key Decisions

1. **Threshold: 2x Celery hard task time limit** (~2 hours with current
   settings). Conservative — gives plenty of room for legitimately slow
   large batches. If the task was killed by Celery's hard time limit at 1h,
   waiting until 2h gives a wide margin. The threshold is computed from
   `settings.CELERY_TASK_TIME_LIMIT * 2`, not hardcoded. The age check
   uses the oldest QUEUED alert's `ingest_time` as a proxy, since the
   Alert model has no `updated_at` or `queued_at` field. This is safe:
   `ingest_time` is always older than the actual queued time, so recovery
   triggers sooner than intended, never later.

2. **Location: inside dispatch_crossmatch_batch** — modify the concurrency
   guard to check QUEUED alert age before deciding to skip. Minimal change
   to existing code.

3. **Observability: log warning only** — a structured log warning with the
   count of recovered alerts and the age of the oldest one. No model changes
   or persistent tracking needed.

4. **Recovery action: revert QUEUED → INGESTED** — same as the existing
   crossmatch_batch exception handler. Alerts re-enter the normal pipeline
   and will be picked up in the next batch dispatch.

5. **No retry limit** — alerts can be recovered and re-queued indefinitely.
   If crossmatch keeps failing, the alerts will cycle between INGESTED and
   QUEUED, which is visible in logs. A retry limit could be added later if
   needed but is YAGNI for now.

## Open Questions

None — all decisions resolved during brainstorming.
