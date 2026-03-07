---
title: "Lowercase PostgreSQL column names"
topic: database schema
date: 2026-03-07
---

# Brainstorm: Lowercase PostgreSQL column names

## Problem

Two column names in the current schema are mixed-case:

- `lsst_diaObject_diaObjectId` — primary key of `alerts`; FK column in `alert_deliveries`, `catalog_matches`, `crossmatch_runs`, `notifications`
- `lsst_diaSource_diaSourceId` — nullable field in `alerts`

PostgreSQL is case-insensitive for unquoted identifiers, but preserves case when columns are created with quoted names. Because Django issues `CREATE TABLE` with those names quoted, psql and other direct DB tools require quoting them on every use, which is tedious.

All other columns in the schema are already lowercase snake_case and are unaffected.

## What We're Building

Update the design document and the Django models so that both columns are stored in PostgreSQL with all-lowercase names:

| Current name | New DB column name |
|---|---|
| `lsst_diaObject_diaObjectId` | `lsst_diaobject_diaobjectid` |
| `lsst_diaSource_diaSourceId` | `lsst_diasource_diasourceid` |

The naming style is **squish camelCase to lowercase** — a purely mechanical, reversible mapping that preserves the LSST schema concept names without introducing new abbreviations.

## Key Decisions

### 1. Naming convention: squish camelCase

`lsst_diaobject_diaobjectid` (not `lsst_dia_object_dia_object_id`, not `dia_object_id`). Rationale: stays as close as possible to the upstream LSST identifiers while satisfying the PostgreSQL usability goal.

### 2. Python attribute names stay unchanged

The Django model fields keep their current mixed-case Python names (`lsst_diaObject_diaObjectId`, `lsst_diaSource_diaSourceId`). A `db_column` override is added to each field to map to the new lowercase DB column. This means **zero churn in application Python code** — all ORM queries, task signatures, normalizers, and consumers are unchanged.

```python
# Before
lsst_diaObject_diaObjectId = models.TextField(unique=True)

# After
lsst_diaObject_diaObjectId = models.TextField(
    unique=True,
    db_column='lsst_diaobject_diaobjectid',
)
```

### 3. FK db_column overrides updated

`AlertDelivery`, `CatalogMatch`, `CrossmatchRun`, and `Notification` all carry an explicit `db_column='lsst_diaObject_diaObjectId'` on their FK to `Alert`. These need to change to `db_column='lsst_diaobject_diaobjectid'` to match the renamed PK column.

### 4. Migration approach: Django AlterField

Changing `db_column` on an existing field causes Django to generate an `ALTER TABLE … RENAME COLUMN` for PostgreSQL. A Django migration handles this cleanly across all affected tables. For dev environments the DB can also be wiped and recreated, but the migration must exist for correctness in any environment.

## Scope

Files that change:

| File | Change |
|---|---|
| `scimma_crossmatch_service_design.md` | Update column names in §5.2 tables |
| `crossmatch/core/models.py` | Add `db_column=` to 2 Alert fields; update 4 FK `db_column=` values |
| `crossmatch/core/migrations/` | New migration: `AlterField` for each affected field |

Files that do **not** change:

- `brokers/__init__.py`, `brokers/normalize.py`, `brokers/antares/consumer.py`, `brokers/lasair/consumer.py`
- `tasks/crossmatch.py`
- `matching/gaia.py`, `matching/constraints.py`
- `docker/docker-compose.yaml`, Kubernetes manifests, settings

## Open Questions

None — scope and approach are fully decided.
