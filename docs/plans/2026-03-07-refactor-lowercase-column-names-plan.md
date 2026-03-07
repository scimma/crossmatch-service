---
title: "refactor: Lowercase PostgreSQL column names"
type: refactor
date: 2026-03-07
---

# ♻️ refactor: Lowercase PostgreSQL column names

## Overview

Two column names in the current schema are stored with mixed case, making them
awkward to use directly in `psql` (they require quoting everywhere):

| Current DB column | New DB column |
|---|---|
| `lsst_diaObject_diaObjectId` | `lsst_diaobject_diaobjectid` |
| `lsst_diaSource_diaSourceId` | `lsst_diasource_diasourceid` |

The fix is **narrow**: add `db_column=` overrides to the two `Alert` model
fields and update the four FK `db_column=` values that reference the PK column.
Python attribute names are **unchanged** — zero churn in application code.

## Source

Brainstorm: `docs/brainstorms/2026-03-07-lowercase-column-names-brainstorm.md`

## Implementation Plan

### Phase 1: Update design document

- [x] In `scimma_crossmatch_service_design.md` §5.2, rename the two columns
  in every table that references them:

  | Table | Old | New |
  |---|---|---|
  | `alerts` (PK) | `lsst_diaObject_diaObjectId` | `lsst_diaobject_diaobjectid` |
  | `alerts` | `lsst_diaSource_diaSourceId` | `lsst_diasource_diasourceid` |
  | `alert_deliveries` (FK) | `lsst_diaObject_diaObjectId` | `lsst_diaobject_diaobjectid` |
  | `catalog_matches` (FK) | `lsst_diaObject_diaObjectId` | `lsst_diaobject_diaobjectid` |
  | `crossmatch_runs` (FK) | `lsst_diaObject_diaObjectId` | `lsst_diaobject_diaobjectid` |
  | `notifications` (FK) | `lsst_diaObject_diaObjectId` | `lsst_diaobject_diaobjectid` |

### Phase 2: Update models.py

- [x] Add `db_column='lsst_diaobject_diaobjectid'` to `Alert.lsst_diaObject_diaObjectId`
- [x] Add `db_column='lsst_diasource_diasourceid'` to `Alert.lsst_diaSource_diaSourceId`
- [x] Change `db_column='lsst_diaObject_diaObjectId'` →
  `db_column='lsst_diaobject_diaobjectid'` on the FK in each of:
  - `AlertDelivery.alert`
  - `CatalogMatch.alert`
  - `CrossmatchRun.alert`
  - `Notification.alert`

  Note: `to_field='lsst_diaObject_diaObjectId'` stays unchanged — it is a
  Python attribute name reference, not a DB column name.

```python
# crossmatch/core/models.py (Alert model, after change)

lsst_diaObject_diaObjectId = models.TextField(
    unique=True,
    null=False,
    db_column='lsst_diaobject_diaobjectid',
)
lsst_diaSource_diaSourceId = models.TextField(
    null=True,
    db_column='lsst_diasource_diasourceid',
)

# FK fields (AlertDelivery, CatalogMatch, CrossmatchRun, Notification)
alert = models.ForeignKey(
    Alert,
    to_field='lsst_diaObject_diaObjectId',   # ← unchanged
    on_delete=models.CASCADE,
    db_column='lsst_diaobject_diaobjectid',  # ← lowercase
)
```

### Phase 3: Update initial migration

- [x] Update `0001_initial.py` to include `db_column=` on the two Alert fields
  and all four FK fields — so `CREATE TABLE` uses lowercase names from the
  start. No `0002` migration needed since there is no existing database state.

## Acceptance Criteria

- [ ] `scimma_crossmatch_service_design.md` §5.2 uses `lsst_diaobject_diaobjectid`
  and `lsst_diasource_diasourceid` in all six table definitions
- [ ] `models.py` `Alert` fields have `db_column=` overrides for both columns
- [ ] All four FK `db_column=` values are updated to lowercase
- [ ] `to_field='lsst_diaObject_diaObjectId'` values are unchanged
- [ ] A migration `0002_lowercase_column_names.py` exists with only `AlterField`
  operations (no destructive `DROP`/`ADD` column pairs)
- [ ] After applying the migration, `\d core_alert` in psql shows
  `lsst_diaobject_diaobjectid` and `lsst_diasource_diasourceid` without quotes
- [ ] All existing Python application code (brokers, tasks, normalizers) runs
  without modification

## Dependencies & Risks

**Migration rename order**: The PK column in `alerts` must be renamed before
(or in the same transaction as) the FK columns in the child tables. Django
handles this automatically within a single migration, but verify the operation
order in the generated file.

**FK constraints**: PostgreSQL may drop and recreate FK constraints during the
rename. The migration should handle this automatically; verify no orphaned
constraints remain after applying.

**Dev database**: Wipe the Docker volume and recreate rather than running the
migration if the existing dev data is disposable. The migration must still
exist for correctness in shared or production environments.

## Files Changed

| File | Change |
|---|---|
| `scimma_crossmatch_service_design.md` | Rename columns in §5.2 table definitions |
| `crossmatch/core/models.py` | Add `db_column=` to 2 fields; update 4 FK `db_column=` |
| `crossmatch/core/migrations/0001_initial.py` | Add `db_column=` to 2 Alert fields; update 4 FK `db_column=` |

**No other files change** — Python attribute names are unchanged throughout.

## References

- Brainstorm: `docs/brainstorms/2026-03-07-lowercase-column-names-brainstorm.md`
- Models: `crossmatch/core/models.py`
- Design §5.2: Database schema tables
