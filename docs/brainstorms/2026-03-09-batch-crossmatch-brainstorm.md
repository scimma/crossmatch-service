---
title: "Batch crossmatch processing for LSDB performance"
type: feature
date: 2026-03-09
---

# Batch crossmatch processing for LSDB performance

## What We're Building

LSDB crossmatching performs significantly better when alerts are processed as a
batch rather than individually. We are replacing the current per-alert
`crossmatch_alert` Celery task with a batch model that accumulates ingested
alerts and processes them together.

Two tunable environment variables control when a batch fires:

| Variable | Default | Description |
|---|---|---|
| `CROSSMATCH_BATCH_MAX_WAIT_SECONDS` | `900` (15 min) | Maximum wall-clock time before a batch is dispatched |
| `CROSSMATCH_BATCH_MAX_SIZE` | `100000` | Maximum number of alerts that can accumulate before a batch is dispatched |

A batch is dispatched when **either** threshold is reached, whichever comes first.

## Why This Approach

LSDB uses HEALPix-partitioned catalogs with Dask parallelism. Batch processing
allows LSDB to:
- Amortize partition I/O across many alerts (read each Gaia partition once per batch)
- Build more efficient Dask task graphs
- Reduce per-alert overhead from repeated catalog opens

## Key Decisions

### 1. Trigger mechanism: Celery Beat periodic task

A Celery Beat task runs on a **short fixed interval** (e.g., every 30–60
seconds). On each tick it checks:

1. Count of alerts with `status='INGESTED'` — if >= `CROSSMATCH_BATCH_MAX_SIZE`,
   dispatch a batch.
2. Age of the oldest `INGESTED` alert (via `ingest_time`) — if >=
   `CROSSMATCH_BATCH_MAX_WAIT_SECONDS`, dispatch a batch.
3. If neither threshold is met, do nothing and wait for the next tick.

The short interval ensures that the count threshold triggers promptly (within
30–60 s of accumulating enough alerts) rather than waiting for the full
wall-clock window.

### 2. Batch scope: query by status

The existing `Alert.status` field (`INGESTED → QUEUED → MATCHED → NOTIFIED`)
is used to identify batch membership:

- The Beat task queries for alerts with `status='INGESTED'`.
- When a batch is dispatched, those alerts transition to `status='QUEUED'`.
- The batch crossmatch task queries for `status='QUEUED'` alerts and processes
  them.

No new models or Redis data structures are needed.

### 3. Overflow: cap at max_batch_size

When more than `CROSSMATCH_BATCH_MAX_SIZE` alerts are pending, the Beat task
transitions at most `max_batch_size` alerts (ordered by `ingest_time`) to
`QUEUED` and dispatches one batch task. Remaining `INGESTED` alerts wait for
the next Beat tick. This keeps batch sizes predictable and memory-bounded.

### 4. Task signature: no args, query in task

The new task signature is:

```python
crossmatch_batch(match_version: int = 1)
```

The Beat task transitions alerts from `INGESTED` → `QUEUED` and then enqueues
`crossmatch_batch`. The task itself queries for `QUEUED` alerts when it runs.
This avoids passing large ID lists through Redis.

Status transitions provide coordination:
- Beat task: `UPDATE alerts SET status='QUEUED' WHERE status='INGESTED' ORDER BY ingest_time LIMIT max_batch_size`
- Batch task: `SELECT * FROM alerts WHERE status='QUEUED'`

### 5. Replace per-alert task entirely

The existing `crossmatch_alert(diaObjectId)` task is removed. Ingest no longer
enqueues per-alert Celery tasks. The Beat scheduler is the sole path to
crossmatching — one code path, simpler system.

`ingest_alert()` will be updated to only UPSERT the alert and record the
delivery. It no longer calls `.delay()`.

### 6. Ingest changes

`ingest_alert()` currently enqueues `crossmatch_alert.delay()` on first
delivery. With batching:
- Remove the `.delay()` call from `ingest_alert()`.
- Alerts are left in `status='INGESTED'` for the Beat task to pick up.
- The first-delivery deduplication gate (`AlertDelivery`) still prevents
  duplicate inserts but no longer triggers immediate task dispatch.

## Environment Variables Summary

| Variable | Default | Notes |
|---|---|---|
| `CROSSMATCH_BATCH_MAX_WAIT_SECONDS` | `900` | 15 minutes |
| `CROSSMATCH_BATCH_MAX_SIZE` | `100000` | Per-batch cap |
| `CROSSMATCH_BATCH_CHECK_INTERVAL` | `30` | Beat tick interval (seconds) |

### 7. Concurrency guard: status-based

Before dispatching a new batch, the Beat task checks if any alerts have
`status='QUEUED'`. If so, a batch is already in progress — skip this tick.

This is the simplest possible guard: no Redis locks, no extra state. The
existing `status` field is the single source of truth. The batch task
transitions alerts from `QUEUED` → `MATCHED` on completion, which clears the
guard for the next tick.

### 8. Failure handling: revert to INGESTED

If the batch crossmatch task fails (LSDB error, worker crash, timeout), all
`QUEUED` alerts are reverted to `INGESTED` so they are picked up by the next
batch dispatch.

The batch task wraps its work in a try/except:
- On success: transition alerts from `QUEUED` → `MATCHED`.
- On failure: transition alerts from `QUEUED` → `INGESTED` and re-raise.

This gives simple automatic retry semantics — failed alerts naturally rejoin
the next batch without manual intervention.

## Open Questions

1. **Batch task timeout**: The current task timeout is 3600 s (1 hour). A batch
   of 100k alerts may need a longer timeout. TBD based on LSDB benchmarking.

## What Does NOT Change

- Alert model fields and schema — no migration needed.
- Ingest normalization and UPSERT logic — unchanged.
- ANTARES and Lasair consumers — unchanged.
- Redis as Celery broker — unchanged.
- `AlertDelivery` deduplication gate — still prevents duplicate inserts.
