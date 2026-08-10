---
title: "CREATE INDEX CONCURRENTLY deadlocks under the locked_init advisory lock with multiple consumer replicas"
date: 2026-07-22
category: docs/solutions/integration-issues/
module: crossmatch/project (locked_init), crossmatch/core/migrations
problem_type: integration_issue
component: database_migration
symptoms:
  - "`manage.py locked_init` hangs at 'Begin Django initialization...' after applying the first migration; the migrate never completes"
  - "All ingest consumer pods (antares/lasair/pittgoogle) sit READY-but-wedged in startup; ingest stops"
  - "A migration is applied (e.g. 0006) but the next ones (0007+) never land in django_migrations"
  - "pg_stat_activity shows a `CREATE INDEX CONCURRENTLY` active for minutes with wait_event_type=Lock, wait_event=virtualxid"
  - "The other consumer sessions are active on `SELECT pg_advisory_lock($1)` and each hold an open snapshot (backend_xmin set)"
  - "No PostgreSQL deadlock error is ever raised -- the migrate just hangs indefinitely"
root_cause: lock_contention
resolution_type: code_fix
severity: high
---

## Summary

When more than one container runs `manage.py locked_init` at startup and a migration
uses `CREATE INDEX CONCURRENTLY` (Django `AddIndexConcurrently`, `atomic = False`), the
migrate deadlocks. The deadlock is a circular wait that PostgreSQL does **not** detect,
so `migrate` hangs forever and every consumer stays wedged in startup. First hit on DEV
deploying 0.9.0 (payload-retention migrations 0006-0009) against a 1.99M-row
`core_alert`.

## Root cause

`locked_init` serialized startup with a **blocking** call:

```python
conn.execute("SELECT pg_advisory_lock(%s)", [LOCK_ID])   # blocks until granted
```

A blocking `pg_advisory_lock()` runs as a **single statement for the entire time it
waits**. A running statement holds a transaction snapshot (`backend_xmin`) open on that
backend -- even with `autocommit=True`, because the snapshot is pinned for the duration
of the statement, not the transaction.

The three ingest consumers race for the one advisory lock at startup. The winner runs
`migrate`; the losers park on `pg_advisory_lock()`, each pinning a snapshot.

`CREATE INDEX CONCURRENTLY` must wait for every transaction whose snapshot predates its
build phases to finish before it can complete. So:

- the migrator's CIC waits for the parked consumers' snapshots to clear, and
- the parked consumers cannot clear their snapshots until they acquire the advisory
  lock, which the migrator will not release until the CIC (i.e. `migrate`) finishes.

Circular wait. `pg_blocking_pids` shows the loop directly
(`migrator-CIC -> consumer-snapshot -> advisory-lock -> migrator`). PostgreSQL's
deadlock detector does not cover a CIC virtualxid wait, so nothing aborts it.

Only the ingest consumers run `locked_init` (via `entrypoints/django_init.sh`);
celery-worker, celery-beat, and web do not, which is why exactly the three consumers
appear in the wait chain.

## Diagnosis (how to confirm it live)

```sql
-- the stuck CIC and the parked snapshot-holders
select pid, state, now()-query_start as running, wait_event_type, wait_event,
       backend_xmin, left(query,60)
from pg_stat_activity
where datname = current_database() and state <> 'idle';

-- the circular wait
select pid, pg_blocking_pids(pid) from pg_stat_activity where state <> 'idle';

-- advisory lock holder
select pid, granted from pg_locks where locktype = 'advisory';
```

CIC on `wait_event = virtualxid` + parked `pg_advisory_lock` sessions with a non-null
`backend_xmin` is the signature.

## Resolution

Poll the **non-blocking** `pg_try_advisory_lock` with a sleep instead of blocking, so a
waiting container sits idle between attempts holding no snapshot
(`crossmatch/project/management/commands/locked_init.py`):

```python
while True:
    granted = conn.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID]).fetchone()[0]
    if granted:
        break
    self.stdout.write("Initialization lock held by another container; retrying...")
    time.sleep(poll_interval)
```

Serialization semantics are unchanged (still blocks until acquired); the difference is
that a waiter no longer pins a snapshot, so the migrator's CIC can finish. Shipped in
0.9.1.

### Breaking an already-wedged environment

The code fix only helps future deploys. To recover a live hang, remove the parked
snapshot-holders so the CIC completes, leaving exactly one `locked_init` runner. With
ArgoCD `selfHeal: true`, first suspend automation or the scale-down is reverted:

```bash
kubectl -n argocd patch application crossmatch-service --type merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
kubectl -n crossmatch-service scale statefulset lasair-consumer antares-consumer --replicas=0
# terminate their now-orphaned (dead-pod) backends if the sessions linger:
#   select pg_terminate_backend(pid) from pg_stat_activity where query like '%pg_advisory_lock%';
# let the surviving migrator finish 0006-0009, verify the ledger, then restore:
kubectl -n argocd patch application crossmatch-service --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
```

## Prevention / related

- Any future `AddIndexConcurrently` / `RunSQL` `CREATE INDEX CONCURRENTLY` under
  `locked_init` is safe now that waiters do not pin snapshots. It is still good practice
  to run concurrent-index migrations in a quiet window: a long-running *application*
  transaction (celery/web) that predates the CIC will also stall it -- that is a normal
  CIC property, independent of this deadlock.
- A stronger structural option (not taken in 0.9.1) is to run schema migrations from a
  single dedicated migration Job / initContainer instead of N racing consumers, which
  removes the race entirely. That is a gitops chart change rather than an image change.
- Same deploy that surfaced this also had an O(n^2) backfill in migration 0008 (it
  re-scanned `notified_at IS NULL` each batch); fixed to a monotonic pk-cursor in the
  same 0.9.1 change. See `crossmatch/core/migrations/0008_backfill_notified_at.py`.
