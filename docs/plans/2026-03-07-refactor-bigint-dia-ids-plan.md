---
title: "refactor: Change diaObject/diaSource ID columns from TEXT to BIGINT"
type: refactor
date: 2026-03-07
---

# ♻️ refactor: Change diaObject/diaSource ID columns from TEXT to BIGINT

## Overview

LSST DIA identifiers are 64-bit integers (e.g. `170055002004914266`). The
current schema stores them as `TEXT`, which requires an unnecessary `str()`
cast in the normalization layer and uses a less efficient index type.
Changing to `BIGINT` aligns the schema with the true data type.

No database migration is needed — the database is always created fresh during
this development stage. `0001_initial.py` is updated in-place.

## Source

Brainstorm: `docs/brainstorms/2026-03-07-bigint-dia-ids-brainstorm.md`

## Implementation Plan

### Phase 1: Update design document

- [x] In `scimma_crossmatch_service_design.md` §5.2.1 (`alerts` table), change:
  - `lsst_diaobject_diaobjectid`: `TEXT UNIQUE NOT NULL` → `BIGINT UNIQUE NOT NULL`
  - `lsst_diasource_diasourceid`: `TEXT NULL` → `BIGINT NULL`

### Phase 2: Update Django model

- [x] In `crossmatch/core/models.py`, change `Alert`:
  - `lsst_diaObject_diaObjectId`: `models.TextField(...)` → `models.BigIntegerField(...)`
  - `lsst_diaSource_diaSourceId`: `models.TextField(null=True, ...)` → `models.BigIntegerField(null=True, ...)`

```python
# crossmatch/core/models.py (Alert, after change)
lsst_diaObject_diaObjectId = models.BigIntegerField(
    unique=True, null=False, db_column='lsst_diaobject_diaobjectid'
)
lsst_diaSource_diaSourceId = models.BigIntegerField(
    null=True, db_column='lsst_diasource_diasourceid'
)
```

### Phase 3: Update initial migration

- [x] In `crossmatch/core/migrations/0001_initial.py`, change both field
  definitions from `models.TextField(...)` to `models.BigIntegerField(...)`,
  preserving existing `db_column=`, `unique=`, and `null=` arguments.

### Phase 4: Remove str() casts in normalizers

- [x] In `crossmatch/brokers/normalize.py`:
  - `normalize_antares()`: remove `str()` around `raw_alert['lsst_diaObject_diaObjectId']`
    and `raw_alert['lsst_diaSource_diaSourceId']`
  - `normalize_lasair()`: remove `str()` around `raw_alert['diaObjectId']`
    (the `lsst_diaSource_diaSourceId = None` line is unchanged)

```python
# crossmatch/brokers/normalize.py (after change)

def normalize_antares(raw_alert: dict) -> dict:
    return {
        'lsst_diaObject_diaObjectId': raw_alert['lsst_diaObject_diaObjectId'],
        ...
        'lsst_diaSource_diaSourceId': raw_alert['lsst_diaSource_diaSourceId'],
        ...
    }

def normalize_lasair(raw_alert: dict) -> dict:
    return {
        'lsst_diaObject_diaObjectId': raw_alert['diaObjectId'],
        ...
    }
```

### Phase 5: Update Celery task type hint

- [x] In `crossmatch/tasks/crossmatch.py`, change the task signature:
  `lsst_diaObject_diaObjectId: str` → `lsst_diaObject_diaObjectId: int`

```python
# crossmatch/tasks/crossmatch.py (after change)
def crossmatch_alert(lsst_diaObject_diaObjectId: int, match_version: int = 1):
```

## Acceptance Criteria

- [ ] `scimma_crossmatch_service_design.md` §5.2.1 shows `BIGINT` for both columns
- [ ] `models.py` uses `BigIntegerField` for both `Alert` fields
- [ ] `0001_initial.py` uses `BigIntegerField` for both fields
- [ ] `normalize_antares()` passes the integer value without `str()` wrapping
- [ ] `normalize_lasair()` passes the integer value without `str()` wrapping
- [ ] `crossmatch_alert` task signature uses `int` type hint
- [ ] No other files require changes (brokers/__init__.py, antares consumer,
  lasair consumer all already handle integers correctly)

## Files Changed

| File | Change |
|---|---|
| `scimma_crossmatch_service_design.md` | §5.2.1 column types TEXT → BIGINT |
| `crossmatch/core/models.py` | `TextField` → `BigIntegerField` for 2 fields |
| `crossmatch/core/migrations/0001_initial.py` | `TextField` → `BigIntegerField` in-place |
| `crossmatch/brokers/normalize.py` | Remove `str()` casts on both IDs |
| `crossmatch/tasks/crossmatch.py` | Type hint `str` → `int` |

## References

- Brainstorm: `docs/brainstorms/2026-03-07-bigint-dia-ids-brainstorm.md`
- Model: `crossmatch/core/models.py:27-29`
- Normalizers: `crossmatch/brokers/normalize.py:13,16,30`
- Task: `crossmatch/tasks/crossmatch.py:9`
