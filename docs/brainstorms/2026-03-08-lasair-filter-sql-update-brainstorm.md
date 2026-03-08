---
title: "Update Lasair filter SQL and consumer normalization"
type: refactor
date: 2026-03-08
---

# Update Lasair filter SQL and consumer normalization

## What We're Building

The Lasair streaming filter `reliability_moderate` has been updated on the
Lasair web UI. The Kafka topic name is unchanged
(`lasair_366SCiMMA_reliability_moderate`). Two things changed in the filter:

1. **`latestR` threshold lowered**: `> 0.7` → `> 0.6` (slightly wider net for
   Real/Bogus quality).
2. **Selected columns changed**: `nDiaSources` and the computed `age` column
   are removed; `firstDiaSourceMjdTai` is added.

The code must be updated so `normalize_lasair()` maps the new column set to the
canonical alert format, and the design document must reflect the new SQL.

## New Filter SQL

```sql
SELECT
    objects.diaObjectId, objects.firstDiaSourceMjdTai, objects.ra, objects.decl
FROM objects
WHERE
    objects.nDiaSources >= 1
    AND objects.latestR > 0.6
    AND mjdnow() - objects.lastDiaSourceMjdTai < 1
```

## Column Diff

| Column | Old | New |
|---|---|---|
| `diaObjectId` | ✅ | ✅ |
| `ra` | ✅ | ✅ |
| `decl` | ✅ | ✅ |
| `nDiaSources` | ✅ selected | ❌ WHERE-only |
| `age` (computed) | ✅ `mjdnow()-lastDiaSourceMjdTai` | ❌ removed |
| `firstDiaSourceMjdTai` | ❌ | ✅ added |

## Key Decision: `event_time` computation

`event_time` in the canonical format previously came from:

```python
datetime.now(tz=timezone.utc) - timedelta(days=raw_alert['age'])
```

This approximated the time of the _last_ detection.

With `age` removed but `firstDiaSourceMjdTai` now available:

**Chosen approach**: convert `firstDiaSourceMjdTai` (MJD TAI) to a UTC
`datetime`. MJD epoch is 1858-11-17 00:00:00 UTC. TAI lags UTC by ~37 s today
(leap seconds), which is negligible for alert-processing use cases.

```python
_MJD_EPOCH = datetime(1858, 11, 17, tzinfo=timezone.utc)
event_time = _MJD_EPOCH + timedelta(days=raw_alert['firstDiaSourceMjdTai'])
```

This gives the time of the object's _first_ detection rather than its most
recent one, which is slightly different semantically from the old value but is
meaningful and stable (the first detection time never changes).

The constant `_MJD_EPOCH` lives in `normalize.py` as a module-level constant.

## Files to Change

| File | Change |
|---|---|
| `scimma_crossmatch_service_design.md` | §2.1 B2: update SQL block, column table, criteria semantics, §10 open-question note |
| `crossmatch/brokers/lasair/consumer.py` | Update module docstring (filter criteria and columns) |
| `crossmatch/brokers/normalize.py` | Update `normalize_lasair()`: new column set, MJD-based `event_time`, updated docstring |

## What Does NOT Change

- Topic name `lasair_366SCiMMA_reliability_moderate` — unchanged.
- `LASAIR_TOPIC` default in `settings.py`, docker-compose, values.yaml — no change.
- `diaObjectId`, `ra`, `decl` mapping — unchanged.
- `lsst_diaSource_diaSourceId = None` — Lasair still provides no source ID.
- The `payload` field stores the full raw message so any new columns are
  captured automatically; no model or migration changes needed.
