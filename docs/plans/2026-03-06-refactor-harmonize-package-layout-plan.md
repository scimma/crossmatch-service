---
title: "refactor: Harmonize §8.2 package layout with current codebase"
type: refactor
date: 2026-03-06
brainstorm: docs/brainstorms/2026-03-06-harmonize-package-layout-brainstorm.md
---

# refactor: Harmonize §8.2 Package Layout with Current Codebase

## Overview

Two coordinated changes to eliminate the gap between the design document and
the actual codebase:

1. **Design doc update** — rewrite §8.2 in `scimma_crossmatch_service_design.md`
   to reflect settled decisions (package names, project sub-package, Celery
   module location, management commands location, entrypoints directory).

2. **Code rename** — rename the `run_alert_consumer` Django management command
   and its shell entrypoint to `run_antares_ingest`, updating all downstream
   references in Docker Compose and the Helm chart.

The `brokers/` namespace (`brokers/antares/`, `brokers/lasair/`) stays in the
design doc as the planned target layout. A note is added that the current code
has `antares/` at the top level pending that future refactor.

---

## Part 1: Design Doc Updates (`scimma_crossmatch_service_design.md` §8.2)

### 1a. Root package name

Replace `alertmatch/` with `crossmatch/` throughout the layout tree.

### 1b. Project sub-package

Replace `alertmatch_project/` with `project/`. Remove `urls.py`, `asgi.py`,
and `wsgi.py` from the sub-package listing (they do not exist). The comment
`# optional now; useful later for a web UI` moves to a prose note if needed.

### 1c. Celery module

Remove `tasks/celery_app.py`. Add `project/celery.py` with comment
`# Celery app; configured from Django settings`.

### 1d. Management commands location

Move the `management/commands/` block from the root level of `alertmatch/`
into `project/management/commands/`. Add `initialize_periodic_tasks.py` which
exists in the code but was absent from the design.

### 1e. Management command names

Replace `run_antares_ingest.py` placeholder with the actual current names, including:
- `initialize_periodic_tasks.py`
- `run_antares_ingest.py` (post-rename; see Part 2)
- `run_lasair_ingest.py` (planned; not yet created — keep with comment)
- `run_notifier.py`
- `sync_pointings.py`

### 1f. Add `entrypoints/` directory

Add an `entrypoints/` block showing the shell scripts that exist:
```
  entrypoints/
    django_init.sh
    run_antares_ingest.sh    # renamed from run_alert_consumer.sh
    run_celery_beat.sh
    run_celery_worker.sh
    run_flower.sh
    wait-for-it.sh
```

### 1g. Keep `brokers/` as target layout with annotation

Retain `brokers/antares/`, `brokers/lasair/`, and `brokers/normalize.py` in
the layout. Add a comment or inline note stating:
> *(target layout — current code has `antares/` at top level pending this refactor)*

### 1h. Also update §8.3 Key Processes

§8.3 references `python manage.py run_antares_ingest` — this already matches
the post-rename name, so no change needed there.

---

## Part 2: Code Rename (`run_alert_consumer` → `run_antares_ingest`)

### 2a. Rename management command file

```
crossmatch/project/management/commands/run_alert_consumer.py
  → run_antares_ingest.py
```

Inside the file, update:
- Class name: `Command` body (the `help` string and any logging)
- No other class rename needed since Django uses the filename as the command name.

### 2b. Rename entrypoint shell script

```
crossmatch/entrypoints/run_alert_consumer.sh
  → run_antares_ingest.sh
```

Inside the script, update the `python manage.py run_alert_consumer` invocation
to `python manage.py run_antares_ingest`. Keep all other content unchanged.

### 2c. Update Docker Compose

File: `docker/docker-compose.yaml`

Find the alert-consumer service entrypoint reference and update:
```yaml
# before
./entrypoints/run_alert_consumer.sh
# after
./entrypoints/run_antares_ingest.sh
```

### 2d. Update Helm statefulset

File: `kubernetes/charts/crossmatch-service/templates/statefulset.yaml`

Find the container command referencing `run_alert_consumer.sh` and update to
`run_antares_ingest.sh`.

---

## Acceptance Criteria

### Design doc
- [x] §8.2 root shows `crossmatch/` not `alertmatch/`
- [x] §8.2 shows `project/` not `alertmatch_project/`; no `urls.py`/`asgi.py`/`wsgi.py`
- [x] `project/celery.py` shown; `tasks/celery_app.py` removed
- [x] Management commands shown under `project/management/commands/`
- [x] `initialize_periodic_tasks.py` present in commands list
- [x] `run_antares_ingest.py` shown (not `run_alert_consumer.py`)
- [x] `entrypoints/` block present with correct shell script names
- [x] `brokers/` block kept with annotation noting current flat layout
- [x] `run_lasair_ingest.py` kept with "planned" annotation

### Code
- [x] `run_alert_consumer.py` renamed to `run_antares_ingest.py`; `python manage.py run_antares_ingest` works
- [x] `run_alert_consumer.sh` renamed to `run_antares_ingest.sh`; script invokes correct command
- [x] `docker-compose.yaml` references `run_antares_ingest.sh`
- [x] Helm `statefulset.yaml` references `run_antares_ingest.sh`
- [x] No remaining references to `run_alert_consumer` in tracked files

---

## References

- Brainstorm: `docs/brainstorms/2026-03-06-harmonize-package-layout-brainstorm.md`
- Design document: `scimma_crossmatch_service_design.md`
- Management command: `crossmatch/project/management/commands/run_alert_consumer.py`
- Shell entrypoint: `crossmatch/entrypoints/run_alert_consumer.sh`
- Docker Compose: `docker/docker-compose.yaml`
- Helm statefulset: `kubernetes/charts/crossmatch-service/templates/statefulset.yaml`
