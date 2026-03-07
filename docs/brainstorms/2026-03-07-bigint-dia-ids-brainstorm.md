---
title: "Change diaObject/diaSource ID columns from TEXT to BIGINT"
topic: database schema
date: 2026-03-07
---

# Brainstorm: Change diaObject/diaSource ID columns from TEXT to BIGINT

## Problem

`lsst_diaobject_diaobjectid` and `lsst_diasource_diasourceid` are defined as
`TEXT` in the design and models, but LSST DIA identifiers are 64-bit integers
in practice (e.g. `170055002004914266`). The normalization layer currently
wraps them in `str()` before storage, which is unnecessary overhead and
obscures the true type. Storing them as `BIGINT` is more accurate, more
efficient for indexing, and avoids any accidental string comparison bugs.

## What We're Building

Change both columns in the design document, Django models, initial migration,
and normalization code:

| Column | Old type | New type |
|---|---|---|
| `lsst_diaobject_diaobjectid` | `TEXT UNIQUE NOT NULL` | `BIGINT UNIQUE NOT NULL` |
| `lsst_diasource_diasourceid` | `TEXT NULL` | `BIGINT NULL` |

## Key Decisions

### 1. Both columns become BIGINT

`lsst_diaobject_diaobjectid` is always an integer from both ANTARES and
Lasair. `lsst_diasource_diasourceid` is currently `NULL` for all Lasair
alerts and an integer from ANTARES when present — `BIGINT NULL` is correct
for both cases.

### 2. Django field: BigIntegerField

`models.BigIntegerField` maps to `BIGINT` in PostgreSQL. The nullable variant
(`null=True`) is used for `lsst_diasource_diasourceid`.

### 3. Remove str() casts in normalizers

`normalize_antares()` and `normalize_lasair()` currently wrap the ID values in
`str()`. These casts are removed so integers flow through unchanged.

### 4. Celery task type hint updated

`crossmatch_alert(lsst_diaObject_diaObjectId: str, ...)` becomes
`crossmatch_alert(lsst_diaObject_diaObjectId: int, ...)`.

### 5. 0001_initial.py updated in-place

No migration file is needed — the database is always created fresh during this
development stage. `0001_initial.py` is updated to use `BigIntegerField`
directly.

## Scope

Files that change:

| File | Change |
|---|---|
| `scimma_crossmatch_service_design.md` | Update §5.2 column types to `BIGINT` |
| `crossmatch/core/models.py` | `TextField` → `BigIntegerField` for both fields |
| `crossmatch/core/migrations/0001_initial.py` | `TextField` → `BigIntegerField` in-place |
| `crossmatch/brokers/normalize.py` | Remove `str()` casts on both IDs |
| `crossmatch/tasks/crossmatch.py` | Type hint `str` → `int` |

Files that do **not** change:

- `brokers/__init__.py` — just passes the value through; ORM accepts int
- `brokers/antares/consumer.py` — mock already uses integers
- `brokers/lasair/consumer.py` — passes integer from Kafka message directly

## Open Questions

None — scope and approach are fully decided.
