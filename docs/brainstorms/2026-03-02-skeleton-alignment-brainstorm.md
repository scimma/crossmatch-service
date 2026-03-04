# Brainstorm: Aligning Skeleton Code to Design Document

**Date**: 2026-03-02
**Status**: Complete

---

## What We're Building

A gap analysis and remediation plan to bring the existing skeleton codebase into alignment with the `lsst_lsdb_design.md` architecture. The skeleton was copied from another project and needs to be reshaped to implement the LSST alert ingestion → LSDB/Gaia crossmatch → notification pipeline.

---

## Design Summary

`lsst_lsdb_design.md` specifies:
- ANTARES broker → Ingest Service → Celery (Redis) → Crossmatch Workers (LSDB/Gaia) → Notifier
- **Django ORM** for models and migrations
- **Redis** as both Celery broker and result backend (no RabbitMQ)
- Five DB tables: `alerts`, `gaia_matches`, `planned_pointings`, `crossmatch_runs`, `notifications`
- Five key processes: ingest, celery-worker, celery-beat, schedule-sync, notifier
- Package layout: `crossmatch/` with sub-packages `antares/`, `heroic/`, `matching/`, `notifier/`, `tasks/`

---

## Skeleton Status

### Well Aligned

- **`Alert` model**: All fields from design §5.2.1 are present (`lsst_diaObject_diaObjectId`, `ra_deg`, `dec_deg`, `event_time`, `payload` JSONB, `status` with correct lifecycle values)
- **Management command pattern**: `run_alert_consumer` matches design's `run_antares_ingest` approach
- **`QueryHEROIC` periodic task**: Placeholder exists for `refresh_planned_pointings`
- **Kubernetes/Helm structure**: Worker resource allocation (up to 48GB RAM) aligns with LSDB/Dask requirements
- **Docker Compose**: All required services present (postgres, redis, worker, beat)
- **Mock alert generator**: Generates realistic LSST DIA fields; useful for early integration testing

### Gaps and Divergences

| Area | Design Spec | Skeleton | Severity |
|---|---|---|---|
| **Celery broker** | Redis | RabbitMQ (AMQP) | High |
| **Missing DB tables** | 5 tables | 1 table (`alerts` only) | High |
| **Crossmatch logic** | Full LSDB/Gaia pipeline | Stub (logs payload only) | High |
| **Notifier service** | Full module + management command | Not present | High |
| **HEROIC client** | `heroic/client.py` + `schedule_sync.py` | Periodic task stub only | Medium |
| **Task signature** | `crossmatch_alert(lsst_diaObject_diaObjectId, match_version)` | `crossmatch(alert_id)` (UUID) | Medium |
| **Module/package layout** | `crossmatch/antares/heroic/matching/notifier/tasks/` | `crossmatch/alerts/core/tasks/` | Medium |
| **Key libraries** | `psycopg` v3, `httpx`, `astropy`, `structlog`, `lsdb` | `psycopg2-binary` v2, none of the others | Medium |
| **ANTARES integration** | `StreamingClient` with reconnect/backpressure | Mock generator only | Low (expected) |

---

## Key Decisions Made

1. **Broker**: Switch from RabbitMQ to Redis as Celery broker (follows design, reduces operational footprint, removes RabbitMQ dependency from Docker Compose and Helm chart)

2. **Package layout**: Full refactor to `crossmatch/` layout with `antares/`, `heroic/`, `matching/`, `notifier/`, `tasks/` sub-packages

3. **Task key**: Switch `crossmatch_alert` to use `lsst_diaObject_diaObjectId` (natural LSST key) rather than internal UUID, for idempotency and durability

---

## Recommended Changes (Ordered by Priority)

### Priority 1: Structural fixes (blocking everything else)

1. **Replace RabbitMQ with Redis as Celery broker**
   - Update `project/celery.py` broker URL to `redis://redis:6379/0`
   - Update `docker-compose.yaml` to remove RabbitMQ service; expose Redis port
   - Update Helm `values.yaml` to remove RabbitMQ dependency, configure Redis broker URL
   - Update `settings.py` `CELERY_BROKER_URL`

2. **Refactor package layout to match design**
   - Rename top-level package `crossmatch/` → `crossmatch/`
   - Rename `project/` → `crossmatch_project/`
   - Rename `alerts/` → `antares/` (contains `ingest.py`, `normalize.py`)
   - Add `heroic/` package (stub `client.py`, `schedule_sync.py`)
   - Add `matching/` package (stub `gaia.py`, `constraints.py`)
   - Add `notifier/` package (stub `watch.py`, `lsst_return.py`)
   - Move `tasks/tasks.py` → `tasks/crossmatch.py` + `tasks/schedule.py`

3. **Add missing Django models and migrations**
   - `PlannedPointing` (design §5.2.2)
   - `GaiaMatch` (design §5.2.3)
   - `CrossmatchRun` (design §5.2.4)
   - `Notification` (design §5.2.5)

### Priority 2: Task correctness

4. **Fix Celery task signature**
   - Rename `crossmatch` → `crossmatch_alert`
   - Change parameter from `alert_id` (UUID) to `lsst_diaObject_diaObjectId: str` + `match_version: int = 1`
   - Update consumer to call `crossmatch_alert.delay(lsst_diaObject_diaObjectId=..., match_version=1)`

5. **Add `crossmatch_runs` tracking to ingest path**
   - In consumer: UPSERT `CrossmatchRun` with `state=queued` before enqueuing
   - In worker: mark `state=running` on pickup, `succeeded/failed` on completion

### Priority 3: Update dependencies

6. **Update `requirements.base.txt`**
   - Replace `psycopg2-binary` with `psycopg[binary]` (v3)
   - Add: `lsdb`, `httpx`, `astropy`, `structlog`, `prometheus-client`
   - Keep: `celery`, `redis`, `django`, `django-celery-beat`, `django-celery-results`

7. **Replace basic logging with `structlog`**
   - Update `core/log.py` to use structlog
   - Update `settings.py` logging config

---

## Open Questions (From Design §10)

1. What LSST return channel do we implement first? (HTTP endpoint? Kafka? Rubin API?)
2. What are the exact ANTARES topic name and auth configuration fields?
3. What initial match radius (arcsec) and which Gaia columns go in `gaia_payload`?
4. Do we skip crossmatch if alert is outside planned HEROIC footprint, or just annotate?
5. What are the HEROIC API endpoint paths, pagination, and auth?

---

## Implementation Milestone (Design §11.2)

The design describes a first working end-to-end milestone. The current plan (Phases 1–5) covers the structural refactoring and scaffolding needed to reach this milestone. The core implementations below are deferred to future work:

- Ingest mock alert → store in DB → enqueue Celery task ✅ in scope (Phases 1–4)
- Worker: crossmatch against Gaia with fixed radius → store `GaiaMatch` row ⏳ deferred
- Notifier: dummy implementation (logs payload) + `Notification` bookkeeping ⏳ deferred
- Schedule sync: refresh `PlannedPointing` table from HEROIC, worker annotates "in planned footprint" ⏳ deferred
