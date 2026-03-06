---
date: 2026-03-06
topic: harmonize §8.2 package layout between design doc and current code
branch: main
---

# Brainstorm: Harmonize §8.2 Package Layout

## What We're Building

A two-part harmonization of `scimma_crossmatch_service_design.md` §8.2 and
the actual codebase under `crossmatch/`:

1. **Design doc updates** — correct the documented layout to reflect settled
   decisions (package names, project sub-package, management command location,
   Celery module placement).
2. **Code change** — rename the `run_alert_consumer` management command to
   `run_antares_ingest` to match the design's broker-specific naming
   convention and align with the planned `run_lasair_ingest` companion.

The `brokers/` namespace (`brokers/antares/`, `brokers/lasair/`,
`brokers/normalize.py`) is the planned target layout and stays in the design
doc as-is — a code refactor to reach that state is future work and is tracked
separately.

---

## Discrepancy Inventory

### 1. Root package name — settled, update doc

| Design doc | Actual code | Decision |
|---|---|---|
| `alertmatch/` | `crossmatch/` | Keep `crossmatch/`. Update doc. |

### 2. Project sub-package name — settled, update doc

| Design doc | Actual code | Decision |
|---|---|---|
| `alertmatch_project/` | `project/` | Keep `project/`. Update doc. |

The design shows `urls.py`, `asgi.py`, and `wsgi.py` inside this package.
These do not exist in the actual `project/` directory. Remove them from the
doc (or mark as optional stubs not yet created).

### 3. Celery app module location — settled, update doc

| Design doc | Actual code | Decision |
|---|---|---|
| `tasks/celery_app.py` | `project/celery.py` | Update doc to show `project/celery.py`. Remove `celery_app.py` from `tasks/`. |

### 4. `brokers/` namespace — keep as planned target, no doc change

| Design doc | Actual code | Decision |
|---|---|---|
| `brokers/antares/`, `brokers/lasair/`, `brokers/normalize.py` | `antares/` at top level | Keep design as-is. It represents the target state; the code refactor is future work. |

### 5. Management commands location — settled, update doc

| Design doc | Actual code | Decision |
|---|---|---|
| `management/commands/` at root level of `alertmatch/` | `project/management/commands/` | Update doc to show `project/management/commands/`. |

### 6. `run_alert_consumer` command name — code rename required

| Design doc | Actual code | Decision |
|---|---|---|
| `run_antares_ingest.py` | `run_alert_consumer.py` | Rename the file and class in the codebase to `run_antares_ingest`. Broker-specific naming is correct now that `run_lasair_ingest` is also planned. |

The rename touches:
- `crossmatch/project/management/commands/run_alert_consumer.py` → `run_antares_ingest.py`
- `crossmatch/entrypoints/run_alert_consumer.sh` — the shell entrypoint that
  invokes the management command; update the `manage.py` call inside it. The
  shell filename itself (`run_alert_consumer.sh`) can stay or also be renamed
  for clarity.

### 7. `entrypoints/` directory — update doc to include it

The actual code has `crossmatch/entrypoints/` containing shell scripts
(`run_celery_worker.sh`, `run_celery_beat.sh`, `run_alert_consumer.sh`,
`run_flower.sh`, `django_init.sh`, `wait-for-it.sh`). The design doc does not
show this directory at all. Add it to the layout.

---

## Proposed Layout (after harmonization)

```
crossmatch/
  manage.py
  requirements.base.txt
  entrypoints/
    django_init.sh
    run_alert_consumer.sh     # or rename to run_antares_ingest.sh
    run_celery_worker.sh
    run_celery_beat.sh
    run_flower.sh
    wait-for-it.sh
  project/
    __init__.py
    celery.py                 # Celery app configured from Django settings
    settings.py
    management/
      commands/
        initialize_periodic_tasks.py
        run_antares_ingest.py   # renamed from run_alert_consumer.py
        run_lasair_ingest.py    # planned (not yet created)
        run_notifier.py
        sync_pointings.py
  core/
    __init__.py
    apps.py
    models.py
    migrations/
  brokers/                    # TARGET LAYOUT (code refactor pending)
    __init__.py
    normalize.py
    antares/
      __init__.py
      ingest.py
      normalize.py
    lasair/
      __init__.py
      ingest.py
      normalize.py
  heroic/
    __init__.py
    client.py
    schedule_sync.py
  matching/
    __init__.py
    gaia.py
    constraints.py
  notifier/
    __init__.py
    watch.py
    lsst_return.py
    impl_http.py
  tasks/
    __init__.py
    crossmatch.py
    schedule.py
```

Note: `antares/` (flat, current state) is replaced by `brokers/antares/` in
the layout above since the design doc keeps `brokers/` as the target. A note
should be added indicating the code currently has `antares/` at the top level
pending the refactor.

---

## Changes Required

### Design doc updates (`scimma_crossmatch_service_design.md` §8.2)

1. Replace `alertmatch/` root with `crossmatch/`
2. Replace `alertmatch_project/` with `project/` (remove `urls.py`, `asgi.py`, `wsgi.py`)
3. Move `celery_app.py` from `tasks/` to `project/celery.py`
4. Move management commands from root-level `management/commands/` into `project/management/commands/`
5. Show `run_antares_ingest.py` (not `run_alert_consumer.py`) in the commands list
6. Add `entrypoints/` to the layout
7. Add a note that `brokers/` is the target layout; current code has `antares/` at top level

### Code changes

1. Rename `crossmatch/project/management/commands/run_alert_consumer.py`
   → `run_antares_ingest.py` and update the class name/command string inside.
2. Rename `crossmatch/entrypoints/run_alert_consumer.sh`
   → `run_antares_ingest.sh` and update the `manage.py run_alert_consumer`
   call inside to `manage.py run_antares_ingest`.
3. Update all references to `run_alert_consumer` in:
   - `docker/docker-compose.yaml` (alert-consumer service entrypoint)
   - `kubernetes/charts/crossmatch-service/templates/statefulset.yaml`
     (container command for the ingest container)

---

## Resolved Questions

- **`entrypoints/` shell filename**: rename `run_alert_consumer.sh` →
  `run_antares_ingest.sh`. Update all Docker/Kubernetes references.
- **`core/apps.py`**: does not exist in the current code but is standard
  Django convention. Keep in the design doc as a file that should be created.
- **`tasks/` Celery import**: no changes to `tasks/`; only `project/celery.py`
  is re-documented. No import paths are affected by the layout doc update.
