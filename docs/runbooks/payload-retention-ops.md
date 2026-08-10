# Payload Retention — Operational Runbook (reclaim + guardrail)

Operational steps for the payload-retention feature (U6 reclaim, U7 guardrail). The
app-code half (schema, anchor, sweep, config, backfill) ships in the retention PR;
this runbook covers the parts an operator runs. Plan of record:
`docs/plans/2026-07-21-002-feat-alert-payload-retention-plan.md`.

## What the app ships

- `Alert.notified_at` (terminal-completion anchor), nullable payload columns
  (migration `0006`), the concurrent `notified_at` index (`0007`), and the
  `notified_at` backfill (`0008`).
- A `retention_sweep` Celery Beat task that nulls terminal payloads past the grace,
  bounded by `CROSSMATCH_RETENTION_MAX_ROWS` per run.
- Config: `CROSSMATCH_RETENTION_GRACE_DAYS` (default 30), `_MAX_ROWS` (10000),
  `_INTERVAL_SECONDS` (3600). **DEV overrides the grace to 3 days** — see the wiring
  note below; the default reclaims nothing on DEV (see "Grace must be below the data's
  age").

### Wiring the grace override in gitops (it takes three files, not one)

The chart has **no generic env passthrough** — env vars are enumerated one by one in
`apps/crossmatch-service/templates/_helpers.yaml` (the `crossmatch.env` block, which
reaches `celery-worker`, where the sweep runs). Setting only a key in `values-dev.yaml`
is a **silent no-op**. Overriding the grace mirrors `CROSSMATCH_BATCH_MAX_SIZE` and
touches three files:

1. `templates/_helpers.yaml` — add `CROSSMATCH_RETENTION_GRACE_DAYS` to `crossmatch.env`.
2. `values.yaml` — add the base default `retention_grace_days: 30`. **Required**: without
   a base default, every env renders `CROSSMATCH_RETENTION_GRACE_DAYS=""`, and
   `settings.py`'s `int(os.getenv(...))` raises `ValueError` at startup (crashes PROD).
3. `values-dev.yaml` — override `retention_grace_days: 3`.

Verify with `helm template . -f values.yaml -f values-dev.yaml` (renders `"3"`) and
`-f values-prod.yaml` (renders `"30"`).

> **Deploy caveat — concurrent-index migrations deadlock under a pre-0.9.1
> `locked_init`.** On <=0.9.0 the migrations `0007`/`0009` (`CREATE INDEX
> CONCURRENTLY`) deadlock against the other consumer replicas parked on the
> `locked_init` advisory lock, and the `0008` backfill is O(n^2). Both are fixed in
> 0.9.1. If you hit a wedged migrate, see
> `docs/solutions/integration-issues/create-index-concurrently-deadlocks-under-locked-init.md`.

## Grace must be below the data's age (or retention frees nothing)

The sweep only nulls rows whose anchor is **older than the grace**. If every row in the
table is younger than the grace, the sweep runs and clears **zero** rows — retention is
effectively off. This is exactly what happened on DEV: the `core_alert` table spanned
~3 weeks (oldest ~23 days), so under the 30-day default the sweep fired hourly for ~40
runs and nulled 0 payloads while the PVC kept filling. Dropping the DEV grace to 3 days
made ~1.99M rows droppable immediately.

Rule of thumb per environment: **the grace must be below both (a) the age of the oldest
retained-if-terminal data and (b) the PVC fill-time** (free bytes / growth rate).
Condition (b) is the real one — a grace longer than the fill-time means the volume fills
before anything ages out. The U7 "grace vs measured fill-time" alert below is the
guardrail for exactly this; it is not optional, it is the check that would have caught
the DEV failure before the disk filled. Re-confirm PROD's oldest-alert age and growth
rate give real headroom under the 30-day default as the Rubin alert rate rises.

## U6 — one-time reclaim

Nulling a payload marks the row dead but does not shrink the table files; a reclaim
returns the space to the OS.

Measured DEV state at the 0.9.0 rollout: PVC ~61% used (119 GB of 197 GB), `core_alert`
~110 GB (almost all payload), `core_notification` ~3.5 GB, ~2M alerts. Reclaim is two
steps: drain the payloads (null them), then rewrite the table to return space to the OS.

### Step A — initial backlog drain (the sweep alone is too slow)

The sweep is throttled to `CROSSMATCH_RETENTION_MAX_ROWS` per hourly run (10000 =
240k/day). That is sized for steady state, **not** for a large pre-existing backlog:
draining DEV's ~2M-row backlog at 240k/day would take ~8 days. For a one-time initial
drain, null the past-grace payloads directly in batches. Run against the `django-db`
pod; it is safe to run **with ingest live** (it only touches rows older than the grace;
ingest writes recent rows):

```sql
-- Repeat until 0 rows; one committed batch at a time. Mirror the sweep's predicate
-- exactly (anchor < now() - grace, payload not already null). The notified_at/sent_at
-- partial indexes back the row selection. A batched shell loop that distinguishes a
-- batch error (retry) from 0-rows (done) is the robust form -- a naive loop that
-- treats an error as "done" stops early.
UPDATE core_alert SET payload = NULL
WHERE ctid IN (SELECT ctid FROM core_alert
               WHERE payload IS NOT NULL AND notified_at < now() - interval '3 days'
               LIMIT 20000);
UPDATE core_notification SET payload = NULL
WHERE ctid IN (SELECT ctid FROM core_notification
               WHERE payload IS NOT NULL AND sent_at < now() - interval '3 days'
               LIMIT 20000);
```

Expect this to be **I/O-bound, not fast**: nulling ~110 GB of TOASTed JSONB rewrites
that much data on the Cinder volume (~hours on DEV, `wait_event=DataFileRead`), the same
total work the sweep would spread over ~8 days. Alternatively, temporarily raise
`CROSSMATCH_RETENTION_MAX_ROWS` in gitops and let the hourly sweep do it. Nulling
**grows** the DB (dead tuples + WAL) until step B.

### Step B — VACUUM FULL reclaim (paused-ingest window)

`VACUUM FULL` takes an `ACCESS EXCLUSIVE` lock, so ingest must be paused first. Ingest
is normally **running** on DEV — pause it the same way as the migration recovery:
suspend the ArgoCD app's self-heal, then scale the three consumers to 0 (see
`docs/solutions/integration-issues/create-index-concurrently-deadlocks-under-locked-init.md`
for the exact commands). Then, once step A has nulled the payloads:

```sql
VACUUM FULL core_alert;
VACUUM FULL core_notification;
```

Expect a large drop (from ~61% toward single digits). Verify with `df -h
/var/lib/postgresql/data` inside the `django-db` pod, then re-enable self-heal to restore
the consumers.

**PROD (deferred).** PROD's data is all <30 days old, so nothing is past the 30-day
grace yet and there is **no dead payload to reclaim at cutover** — the reclaim is a
no-op today. Once PROD payload accumulates past 30 days and the sweep starts nulling
it, choose an **online** reclaim (`pg_repack`, which requires installing the
extension — a dependency-pin/infra change) to avoid a long exclusive lock on the live
public service. Do **not** run `VACUUM FULL` on live PROD.

## U7 — disk guardrail (specified here, applied in gitops)

The guardrail is a Prometheus/Grafana alert in the monitoring stack (both envs run
it). No app-side metric is required: kubelet already exports PVC usage
(`kubelet_volume_stats_used_bytes` / `kubelet_volume_stats_capacity_bytes`) for the
`django-db-data` PVC. Add these alert rules in the gitops monitoring stack (separate
commit):

- **Primary — usage threshold (both envs):** warn when
  `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes` for the
  `django-db-data` PVC crosses a configurable threshold (e.g. 0.75), so an
  approaching full volume is acted on before an outage.
- **Early warning — grace vs measured fill-time:** warn when the configured grace
  approaches or exceeds the **measured** fill-time (free bytes / current growth rate
  derived from `predict_linear` on the PVC usage series) — the condition where
  retention frees nothing (the DEV failure). Keying on the *measured* rate is what
  catches the 30-day PROD default silently becoming unsafe as the Rubin alert rate
  rises.

Route both to an actionable channel (not just a Grafana panel).

## Rollback caveat (migration 0006 is irreversible once the sweep runs)

Migration `0006` makes the payload columns nullable. Reversing it emits
`ALTER COLUMN payload SET NOT NULL`, which PostgreSQL rejects once **any** payload is
NULL — i.e. as soon as the retention sweep has cleared one row (PROD ~30 days after
enable, DEV ~3 days). So rollback below `0006` is **unsupported** once the sweep has
run: an emergency downgrade must first re-populate or `DELETE` the null-payload rows.
The data nulling was already permanent (governance decision); this notes that the
*schema* migration is permanent too. (`0008`'s `noop_reverse` leaving `notified_at`
populated is separately fine.)

## Rollout order (per env)

1. Set the grace (three-file wiring above: DEV 3d, PROD 30d) and deploy the image.
   Deploy **0.9.1 or later** — earlier images deadlock on the concurrent-index
   migrations (see the deploy caveat above).
2. Migrations apply on startup (`locked_init`) — `0006` metadata-only, `0007`/`0009`
   concurrent indexes, `0008` backfill (O(n) pk-cursor as of 0.9.1). On a live,
   actively-ingesting table run in a quiet window (the `locked_init` advisory lock
   serializes ingest behind the migrate run). On a large table the backfill still takes
   time (DEV's ~2M rows: minutes).
3. The `retention_sweep` task runs on its interval — but nulls **nothing** unless the
   grace is below the data's age (see "Grace must be below the data's age"). Confirm it
   is actually clearing rows (`total_run_count` rising *and* `payload IS NULL` count
   climbing), not just firing.
4. DEV reclaim: step A (initial backlog drain) then step B (`VACUUM FULL` in a
   paused-ingest window). PROD: reclaim deferred (no dead payload until data ages past
   the grace; use an online reclaim, not `VACUUM FULL`, on the live service).
5. Add the gitops guardrail alert rules — especially the **grace-vs-measured-fill-time**
   alert (U7), the check that catches a grace silently longer than the fill-time.
