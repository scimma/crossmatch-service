---
date: 2026-03-06
topic: align skeleton code to updated design document (refactor/align-skeleton-to-design-2)
branch: refactor/align-skeleton-to-design-2
---

# Brainstorm: Align Skeleton Code to Updated Design Document

## What We're Building

Four coordinated changes to close the gap between the current skeleton code on
`refactor/align-skeleton-to-design-2` and the updated `scimma_crossmatch_service_design.md`:

1. **Models & migrations** — rename `GaiaMatch` → `CatalogMatch`, add `AlertDelivery`
   model, update `Notification` FK, squash all migrations into a single `0001_initial`.

2. **Requirements** — upgrade `psycopg2-binary` → `psycopg` (v3), add `lsdb`, `httpx`,
   `astropy`, `prometheus-client`.

3. **`brokers/` refactor + Lasair stub** — move `antares/` into `brokers/antares/`,
   add `brokers/lasair/` stub consumer and `run_lasair_ingest` management command,
   add supporting Docker Compose service and Kubernetes StatefulSet.

4. **Settings cleanup** — remove duplicate VALKEY/CACHES block, remove dead `cutout`
   template dir and context processor reference.

---

## Key Decisions

### 1. Models & Migrations

**GaiaMatch → CatalogMatch**

The existing `GaiaMatch` model becomes `CatalogMatch` per §5.2.3. Changes:

| Old field | New field | Type change | Notes |
|---|---|---|---|
| `gaia_source_id` | `catalog_source_id` | `BigIntegerField` → `TextField` | Supports any catalog ID format |
| `gaia_ra_deg` | `source_ra_deg` | — | |
| `gaia_dec_deg` | `source_dec_deg` | — | |
| `gaia_payload` | `catalog_payload` | — | |
| *(new)* | `catalog_name` | `TextField NOT NULL` | e.g. `'gaia_dr3'`, `'des_dr2'` |

Updated unique constraint:
`UNIQUE(alert, catalog_name, catalog_source_id, match_version)`

New indexes: `INDEX(catalog_name)`, `INDEX(catalog_source_id)` (replacing `gaia_source_id`).

Use `db_table = 'catalog_matches'` on the Meta class to match the design doc table name.

**New AlertDelivery model** (§5.2.1b):

| field | type | notes |
|---|---|---|
| id | BigAutoField PK | |
| alert | FK → Alert on `lsst_diaObject_diaObjectId` | CASCADE |
| broker | TextField NOT NULL | `'antares'` or `'lasair'` |
| ingest_time | DateTimeField auto_now_add | |

Constraint: `UNIQUE(alert, broker)` — one delivery record per broker per alert.

**Notification FK update**:

`gaia_match = ForeignKey(GaiaMatch, ...)` → `catalog_match = ForeignKey(CatalogMatch, ...)`
with `db_column='catalog_match_id'`.

**Migration strategy: squash**

Since no production DB exists, delete `0001_initial.py` and `0002_add_crossmatch_models.py`
and replace with a single new `0001_initial.py` that creates all five tables in the
correct final state: `alerts`, `alert_deliveries`, `planned_pointings`, `catalog_matches`,
`crossmatch_runs`, `notifications`.

---

### 2. Requirements

Changes to `requirements.base.txt`:

| Action | Package | Notes |
|---|---|---|
| Remove | `psycopg2-binary` | v2, binary, outdated |
| Add | `psycopg[binary]` | v3, matches design §8.1 |
| Add | `lsdb` | HATS catalog crossmatching |
| Add | `httpx` | async HTTP for HEROIC API |
| Add | `astropy` | MJD↔datetime, coordinate helpers |
| Add | `prometheus-client` | observability metrics |
| Remove | `redis` | replaced by `valkey` |
| Add | `valkey` | drop-in for `redis`; matches server branding; resolves the TODO comment |

---

### 3. `brokers/` Refactor + Lasair Stub

**Directory moves**:

```
crossmatch/antares/         → crossmatch/brokers/antares/
  consumer.py                 consumer.py  (also updated — see §3b below)
  publisher.py                publisher.py
  __init__.py                 __init__.py
```

**New files**:

```
crossmatch/brokers/
  __init__.py
  normalize.py              # stub: alert normalization (placeholder)
  lasair/
    __init__.py
    consumer.py             # stub: Lasair Kafka consumer (NotImplementedError)
```

**3a. Import updates**:

- `project/management/commands/run_antares_ingest.py`:
  `from antares.consumer import consume_alerts` → `from brokers.antares.consumer import consume_alerts`
- No settings.py changes needed (`antares` was never in INSTALLED_APPS).

**3b. Mock consumer: adopt atomic ingest pattern**

Update `brokers/antares/consumer.py` to use the §5.3 two-step pattern:

1. Attempt `INSERT INTO alert_deliveries (alert, broker) VALUES (..., 'antares')`
   using Django ORM `get_or_create` (or raw SQL) on the UNIQUE(alert, broker) constraint.
2. If the row already existed → alert already processed by this broker, skip Celery dispatch.
3. Otherwise → insert into `alerts`, create `alert_deliveries` row, dispatch `crossmatch_alert.delay()`.

This exercises the idempotency guarantee end-to-end with mock data.

**3c. New management command**:

Add `project/management/commands/run_lasair_ingest.py` — identical pattern to
`run_antares_ingest.py` but calls `brokers.lasair.consumer.consume_alerts()`.
The Lasair consumer raises `NotImplementedError` for now.

**3d. New entrypoint script**:

Add `entrypoints/run_lasair_ingest.sh` — same structure as `run_antares_ingest.sh`
but invokes `python manage.py run_lasair_ingest`.

**3e. Docker Compose**:

Add `lasair-consumer` service to `docker/docker-compose.yaml` using the same image
and volume mounts as `alert-consumer`, running `bash entrypoints/run_lasair_ingest.sh`.
The stub will raise immediately; this is acceptable for skeleton alignment.

**3f. Kubernetes Helm statefulset**:

Add a `lasair-consumer` StatefulSet block to `kubernetes/charts/crossmatch-service/templates/statefulset.yaml`
guarded by `.Values.lasair_consumer.enabled` (defaulting to `false` in values.yaml),
running `bash entrypoints/run_lasair_ingest.sh`.

---

### 4. Settings Cleanup

**Remove duplicate VALKEY + CACHES block** (lines 98–109 of settings.py):

The VALKEY_SERVICE, VALKEY_PORT, VALKEY_MASTER_GROUP_NAME, VALKEY_OR_SENTINEL, and
CACHES assignments are defined twice. Keep the first block (lines 55–66) and delete
the redundant second block.

**Remove dead `cutout` references**:

- TEMPLATES `DIRS`: change `[os.path.join(APP_ROOT_DIR, 'cutout/templates')]` → `[]`
- TEMPLATES `context_processors`: remove `"cutout.context_processors.user_profile"`

No other settings changes are needed. `tasks` is correctly in both `INSTALLED_APPS`
and `CELERY_IMPORTS`. The broker/result backend configuration is correct post-RabbitMQ
removal.

---

## Files Affected

| File | Change |
|---|---|
| `crossmatch/core/models.py` | Rename GaiaMatch → CatalogMatch; update fields; add AlertDelivery; update Notification FK |
| `crossmatch/core/migrations/0001_initial.py` | Squashed: all 5 tables from scratch |
| `crossmatch/core/migrations/0002_add_crossmatch_models.py` | Deleted |
| `crossmatch/requirements.base.txt` | psycopg2-binary → psycopg[binary]; add lsdb, httpx, astropy, prometheus-client |
| `crossmatch/antares/` | Moved to `crossmatch/brokers/antares/` |
| `crossmatch/brokers/__init__.py` | New |
| `crossmatch/brokers/normalize.py` | New stub |
| `crossmatch/brokers/antares/consumer.py` | Updated imports + atomic ingest pattern |
| `crossmatch/brokers/lasair/__init__.py` | New |
| `crossmatch/brokers/lasair/consumer.py` | New stub |
| `crossmatch/project/settings.py` | Remove duplicate VALKEY block; fix cutout refs |
| `crossmatch/project/management/commands/run_antares_ingest.py` | Update import path |
| `crossmatch/project/management/commands/run_lasair_ingest.py` | New stub |
| `crossmatch/entrypoints/run_lasair_ingest.sh` | New |
| `docker/docker-compose.yaml` | Add lasair-consumer service |
| `kubernetes/.../statefulset.yaml` | Add lasair-consumer StatefulSet block |
| `kubernetes/.../values.yaml` | Add `lasair_consumer.enabled: false` default |

---

## Resolved Decisions

- **`catalog_name` vocabulary** — **Free text** `TextField` with no enum. Adding a new
  catalog (e.g., `'des_dr2'`) requires no migration, only a new string value at runtime.

- **`valkey` Python package** — **Switch now**. Replace `redis` with `valkey` in
  requirements.base.txt. Drop-in compatible; resolves the existing TODO comment.

- **Lasair env vars in docker-compose** — **Add placeholders now**. Include
  `LASAIR_KAFKA_HOST`, `LASAIR_KAFKA_PORT`, and `LASAIR_TOPIC` as commented-out or
  empty entries in the `lasair-consumer` service block and in `values.yaml`. Documents
  the interface even while the consumer raises `NotImplementedError`.

## Remaining Open Questions

- **`alert_deliveries` in Celery task**: Should `tasks/crossmatch.py` query
  `alert_deliveries` (e.g., to log which brokers have processed the alert), or is the
  table only used at ingest time? Proposed answer: ingest-time only for now.
