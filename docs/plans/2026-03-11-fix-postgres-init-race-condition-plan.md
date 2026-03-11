---
title: "Fix PostgreSQL initialization race condition"
type: fix
status: active
date: 2026-03-11
origin: docs/brainstorms/2026-03-11-fix-postgres-init-race-condition-brainstorm.md
---

# Fix PostgreSQL Initialization Race Condition

Two containers (`lasair-consumer`, `alert-consumer`) concurrently run
`django_init.sh` → `python manage.py migrate` against a fresh database, causing `UniqueViolation` errors
on `pg_type_typname_nsp_index` and noisy stacktraces. Replace the retry loop with a
PostgreSQL advisory lock so only one container initializes at a time.

## Proposed Solution

Create a single Django management command `locked_init` that:

1. Opens a raw `psycopg` connection to PostgreSQL using `settings.DATABASES['default']` credentials (with connection retry)
2. Acquires `pg_advisory_lock(LOCK_ID)` — blocks until available
3. Runs `call_command('migrate')` (in-process)
4. Runs `call_command('initialize_periodic_tasks')` (in-process)
5. Runs `call_command('createsuperuser', '--no-input')` with error handling (in-process)
6. Connection closes on exit → lock releases automatically

Then simplify `django_init.sh` to a single call: `python manage.py locked_init`.

### Design Decisions

- **Single command holds the lock for all init steps** — `call_command()` runs
  migrate, periodic tasks, and superuser creation in-process while the raw psycopg
  connection (and its advisory lock) stays open. No subprocess calls needed.
  (see brainstorm: `docs/brainstorms/2026-03-11-fix-postgres-init-race-condition-brainstorm.md`)

- **Connection retry before lock acquisition** — `wait-for-it.sh` only confirms TCP
  port is open, not that the database is ready. The command retries the psycopg
  connection up to 5 times with 2-second backoff before giving up.

- **Hardcoded lock ID** — Use a fixed constant (e.g., `74656372` — arbitrary, with
  comment explaining purpose) rather than hashing a string at runtime.

- **Session-level advisory lock** — `pg_advisory_lock` (not transaction-level
  `pg_advisory_xact_lock`) so the lock persists for the connection lifetime regardless
  of transaction state.

- **Logging around lock** — Log "Acquiring database initialization lock..." before the
  blocking call and "Lock acquired, proceeding..." after, so operators can distinguish
  "waiting for lock" from "hung."

- **Simplify superuser creation** — Replace the 25-line bash regex loop with a Python
  `try`/`except CommandError` that catches "already taken" and logs it as info. Under
  the advisory lock, concurrent creation is impossible.

- **No lock timeout** — If the lock holder crashes, PostgreSQL releases the lock when
  the connection drops. The only failure mode is a live-but-stuck migration, which
  should be addressed at the migration level.

## Acceptance Criteria

- [x] New management command `crossmatch/project/management/commands/locked_init.py`
- [x] Command acquires `pg_advisory_lock` via raw psycopg connection before running init steps
- [x] Command retries psycopg connection up to 5 times (2s backoff) if database not ready
- [x] Command runs migrate, initialize_periodic_tasks, createsuperuser sequentially under lock
- [x] `django_init.sh` simplified to call `python manage.py locked_init` (no retry loop)
- [ ] No `UniqueViolation` stacktraces in logs on cold start with concurrent containers
- [ ] Waiting container logs "Acquiring database initialization lock..." instead of error stacktraces
- [ ] Both containers complete initialization and proceed to their main process

## Affected Files

| File | Change |
|------|--------|
| `crossmatch/project/management/commands/locked_init.py` | **New** — management command with advisory lock |
| `crossmatch/entrypoints/django_init.sh` | **Replace** — remove retry loops, call `locked_init` |

## References

- Brainstorm: `docs/brainstorms/2026-03-11-fix-postgres-init-race-condition-brainstorm.md`
- Current init script: `crossmatch/entrypoints/django_init.sh`
- Existing management commands: `crossmatch/project/management/commands/`
- Database settings: `crossmatch/project/settings.py:136-151`
