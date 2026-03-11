---
title: "Fix PostgreSQL initialization race condition"
type: fix
date: 2026-03-11
---

# Fix PostgreSQL Initialization Race Condition

## What We're Building

Eliminate the race condition where multiple containers (`lasair-consumer`,
`alert-consumer`) concurrently run `django_init.sh` → `python manage.py migrate`
against a fresh database, causing `UniqueViolation` errors on `pg_type_typname_nsp_index`
and noisy stacktraces in the logs.

Replace the current retry loop with a PostgreSQL advisory lock so that only one
container migrates at a time, and others wait cleanly before proceeding.

## Why This Approach

The current `until` retry loop in `django_init.sh` is functional — the second
container retries after 2 seconds and succeeds — but it produces alarming
stacktraces and Django `IntegrityError` messages in every cold-start log.

A PostgreSQL advisory lock is the right fix because:
- Uses infrastructure already present (PostgreSQL) — no new services or volumes
- Advisory locks block cleanly — no polling, no retries, no error output
- Works identically in Docker Compose (dev) and Kubernetes (prod)
- Automatically released if the connection drops (crash safety)
- `psycopg` is already installed, so a Python wrapper is trivial

Alternative approaches considered:
- **Dedicated init service in docker-compose**: Adds a short-lived `db-init`
  service with `depends_on: condition: service_completed_successfully`. Clean
  but docker-compose-specific; doesn't help in Kubernetes without a separate
  init container pattern.
- **File-based flag with polling**: First migrator writes a marker to a shared
  volume; others poll. Requires a shared volume mount and still involves polling.

## Key Decisions

- Use PostgreSQL advisory lock (not file-based or init-service approaches)
- Implement as a Python wrapper script or Django management command (not shell/psql)
- Lock acquired before `migrate`, released after `migrate` completes
- Use a fixed advisory lock ID (e.g., hash of `'django_migrate'`)
- All containers that call `django_init.sh` benefit automatically
- The `until` retry loop in `django_init.sh` will be replaced, not layered on top

## Affected Files

- `crossmatch/entrypoints/django_init.sh` — replace retry loop with call to
  Python wrapper
- New file: Django management command or standalone script for locked migration
- No changes to `docker-compose.yaml` or Kubernetes configs

## Open Questions

None.
