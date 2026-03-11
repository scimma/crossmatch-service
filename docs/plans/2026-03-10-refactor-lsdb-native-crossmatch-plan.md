---
title: "Replace HEROIC Pointing Constraints with LSDB Native Crossmatching"
type: refactor
date: 2026-03-10
---

# Replace HEROIC Pointing Constraints with LSDB Native Crossmatching

## Overview

Remove all HEROIC telescope pointing code, models, configuration, and design-doc references from the crossmatch service. Replace the crossmatch stub with a real implementation using LSDB's `from_dataframe()` + `crossmatch()` API against the Gaia DR3 HATS catalog on S3.

This is a clean-break refactor: HEROIC is fully removed (not feature-flagged), and the crossmatch pipeline becomes self-contained with no external scheduling dependencies.

## Problem Statement / Motivation

The original design assumed HEROIC planned pointings (center + FOV radius) would constrain LSDB spatial queries. After analysis (see `healpix_vs_visit_crossmatch.md`), this is unnecessary:

- Alerts have precise coordinates; visit pointings only give ~3.5 degree field centers
- HATS catalogs use adaptive HEALPix partitioning; LSDB handles partition alignment automatically
- Visit footprints (~9.6 sq deg) span dozens of HATS partitions, loading many tiles with no alerts
- Alerts span multiple visits, nights, and filters
- HEROIC adds an external dependency with its own failure modes

LSDB's `from_dataframe()` converts alerts to an adaptive HEALPix catalog and `crossmatch()` handles spatial alignment — no manual HEALPix grouping or healpy dependency needed.

## Proposed Solution

### Crossmatch Flow (New)

```
1. Batch dispatcher (every 30s) grabs up to BATCH_MAX_SIZE INGESTED alerts
2. Transitions them to QUEUED, enqueues crossmatch_batch(batch_ids) task
3. crossmatch_batch loads alerts by batch_ids (uuid, lsst_diaObject_diaObjectId, ra_deg, dec_deg)
4. Filters NaN coordinates, converts to LSDB catalog via lsdb.from_dataframe()
5. Crossmatches against cached Gaia catalog: alerts_catalog.crossmatch(gaia, ...)
6. Computes results, writes CatalogMatch rows for matched alerts
7. Transitions ALL alerts in batch to MATCHED (matched and unmatched alike)
```

### LSDB API (Confirmed from Documentation)

- **`lsdb.from_dataframe(df, ra_column, dec_column)`** — converts pandas DataFrame to LSDB Catalog with adaptive HEALPix partitioning (orders 0-7). Best for datasets under 1M rows (our max is 100k). Raises `ValueError` on NaN coordinates.
- **`lsdb.open_catalog(path, storage_options=...)`** — loads a HATS catalog from local or S3 path. NOT `read_hats()` (which does not exist). For public S3 buckets, pass `storage_options={'anon': True}`.
- **`catalog.crossmatch(other, n_neighbors=1, radius_arcsec=1.0, suffixes=...)`** — KDTreeCrossmatch by default, `how='inner'` (only matched rows). Returns merged columns with `_dist_arcsec` distance column. Use `suffixes=('_alert', '_gaia')` for deterministic column names.
- **Catalog order matters** — `alerts.crossmatch(gaia)` means each alert gets its nearest Gaia match.
- **HATS adaptive partitioning** — NOT fixed NSIDE; LSDB handles alignment automatically.

## Technical Considerations

- **S3 access** — Gaia DR3 HATS at `s3://stpubdata/hats/gaia/dr3/` is a public bucket. Must pass `storage_options={'anon': True}` to `open_catalog()` to avoid credential lookup timeouts. Needs `s3fs` in requirements.
- **Catalog caching** — `open_catalog()` only loads metadata (partition structure, schema). Cache the Gaia catalog object as a module-level singleton; it is lightweight and the metadata changes on the order of years.
- **Performance** — `from_dataframe()` at 100k rows should be fine per docs ("under 1M rows"). Dask handles internal parallelism. Prototype to confirm.
- **Margin cache** — Verify whether the public Gaia DR3 HATS ships with a margin cache. If it does, `open_catalog()` picks it up automatically. If not, at 1 arcsec radius edge effects are small but should be documented.
- **No healpy dependency** — LSDB handles HEALPix partitioning internally via `from_dataframe()`.
- **Migration safety** — PlannedPointing table drop is safe (no foreign keys reference it, data is ephemeral HEROIC sync data).
- **Unmatched alerts** — `crossmatch()` with `how='inner'` returns only matched rows. Alerts with no Gaia neighbor within the radius are absent from results. The task must compute the set difference of input vs matched alert IDs and still transition all alerts to MATCHED. Downstream, the notifier checks for the existence of a `CatalogMatch` row to determine whether to send a notification.
- **Zombie QUEUED recovery** — If a worker is OOM-killed mid-task, alerts stay QUEUED and the concurrency guard blocks all future batches. Deferred: add a periodic cleanup or startup check that reverts QUEUED alerts older than `2 * CELERY_TASK_TIME_LIMIT` back to INGESTED. Not implemented in this refactor but noted as a follow-up.
- **`CrossmatchRun` model** — Exists in schema but is not used by the current stub or this refactor. Deferred: the model has a per-alert FK but processing is per-batch, so the schema needs redesign before integration. Not addressed in this refactor.

## Acceptance Criteria

### Phase 1: Remove HEROIC, Add LSDB Settings and Implementation

#### Remove HEROIC code, models, and config

- [x] Delete `crossmatch/heroic/` package (`client.py`, `schedule_sync.py`, `__init__.py`)
- [x] Delete `crossmatch/matching/constraints.py` (pointings_covering stub)
- [x] Delete `crossmatch/project/management/commands/sync_pointings.py`
- [x] Remove `PlannedPointing` model from `crossmatch/core/models.py` (lines 81-102)
- [x] Remove `PlannedPointing` from `0001_initial.py` (dev-only DB, no separate drop migration needed)
- [x] Remove `QueryHEROIC` class, `query_heroic` task, and `refresh_planned_pointings` task from `crossmatch/tasks/schedule.py`
- [x] Remove `QueryHEROIC` from `periodic_tasks` list in `crossmatch/tasks/schedule.py`
- [x] Remove `QUERY_HEROIC_INTERVAL` from `crossmatch/project/settings.py` (line 9)
- [x] Remove `QUERY_HEROIC_INTERVAL` env vars from `docker/docker-compose.yaml` (lines 46, 159-161)

#### Add LSDB crossmatch settings

- [x] Add to `crossmatch/project/settings.py`:
  - `GAIA_HATS_URL` (default `s3://stpubdata/hats/gaia/dr3/`)
  - `CROSSMATCH_RADIUS_ARCSEC` (default `1.0`)
- [x] Add env vars to `docker/docker-compose.yaml` celery-worker service:
  - `GAIA_HATS_URL`
  - `CROSSMATCH_RADIUS_ARCSEC`
- [x] Add env vars to `kubernetes/charts/crossmatch-service/values.yaml` crossmatch section
- [x] Add `s3fs` to `crossmatch/requirements.base.txt` (for LSDB S3 access)

#### Implement crossmatch

- [x] Rewrite `crossmatch/matching/gaia.py` — implement `crossmatch_alerts_against_gaia(df)` using:

  ```python
  import lsdb
  import pandas as pd
  from django.conf import settings

  # Module-level cached Gaia catalog (metadata only, lightweight)
  _gaia_catalog = None

  def _get_gaia_catalog():
      global _gaia_catalog
      if _gaia_catalog is None:
          _gaia_catalog = lsdb.open_catalog(
              settings.GAIA_HATS_URL,
              columns=['source_id', 'ra', 'dec'],
              storage_options={'anon': True},
          )
      return _gaia_catalog

  def crossmatch_alerts_against_gaia(alerts_df: pd.DataFrame) -> pd.DataFrame:
      """Crossmatch a DataFrame of alerts against Gaia DR3 via LSDB.

      Args:
          alerts_df: DataFrame with columns including ra_deg, dec_deg.
                     NaN coordinates are filtered before crossmatching.

      Returns:
          DataFrame with matched rows containing merged alert + Gaia columns
          plus _dist_arcsec distance column. Column suffixes: _alert, _gaia.
      """
      # Filter NaN coordinates
      clean_df = alerts_df.dropna(subset=['ra_deg', 'dec_deg'])

      alerts_catalog = lsdb.from_dataframe(
          clean_df, ra_column='ra_deg', dec_column='dec_deg'
      )
      gaia = _get_gaia_catalog()
      matches = alerts_catalog.crossmatch(
          gaia,
          n_neighbors=1,
          radius_arcsec=settings.CROSSMATCH_RADIUS_ARCSEC,
          suffixes=('_alert', '_gaia'),
      )
      return matches.compute()
  ```

- [x] Rewrite `crossmatch/tasks/crossmatch.py` — replace stub with real logic:

  ```python
  @shared_task(name="crossmatch_batch")
  def crossmatch_batch(batch_ids: list, match_version: int = 1) -> None:
      """Process a specific batch of alert IDs through LSDB crossmatch.

      Args:
          batch_ids: List of alert UUIDs to process (passed from dispatcher).
      """
      # 1. Load alerts by batch_ids into DataFrame
      alerts_qs = Alert.objects.filter(pk__in=batch_ids)
      alerts_df = pd.DataFrame(
          alerts_qs.values_list(
              'uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg'
          ),
          columns=['uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg']
      )
      # 2. Crossmatch via LSDB
      result_df = crossmatch_alerts_against_gaia(alerts_df)
      # 3. Write CatalogMatch rows for matched alerts
      #    Map: source_id -> catalog_source_id, ra_gaia -> source_ra_deg,
      #    dec_gaia -> source_dec_deg, _dist_arcsec -> match_distance_arcsec
      # 4. Transition ALL alerts in batch_ids to MATCHED
      #    (both matched and unmatched — downstream checks CatalogMatch existence)
  ```

- [x] Update `dispatch_crossmatch_batch` to pass `batch_ids` to `crossmatch_batch.delay(batch_ids=batch_ids)`
- [x] Write CatalogMatch rows from result DataFrame — use `lsst_diaObject_diaObjectId` for the FK, `source_id` for `catalog_source_id`, `ra`/`dec` (Gaia) for `source_ra_deg`/`source_dec_deg`, `_dist_arcsec` for `match_distance_arcsec`
- [x] Transition ALL alerts in batch to MATCHED (matched and unmatched alike — no alerts left stuck in QUEUED)

#### Verify

- [x] Remove `pseudo-code-healpix-cell-grouping.py` (superseded)
- [x] Verify no remaining HEROIC references: `grep -ri heroic crossmatch/ docker/ kubernetes/`
- [x] Verify no remaining `PlannedPointing` references (only in 0001_initial.py migration, expected)
- [ ] Run migrations against clean database
- [ ] Verify celery-beat starts cleanly with only `DispatchCrossmatchBatch` periodic task

### Phase 2: Update Design Document

- [x] Remove §4.3 (HEROIC Integration)
- [x] Remove §5.2.2 (planned_pointings table)
- [x] Remove §11.1 (HEROIC reference fields)
- [x] Remove HEROIC references in §6 (Ingestion Workers), §8.4 (Deployment), §9 (Configuration), §10 (Open Questions)
- [x] Add LSDB native crossmatch strategy description
- [x] Update §5.3 (Crossmatch Pipeline) with new flow
- [x] Add new settings (`GAIA_HATS_URL`, `CROSSMATCH_RADIUS_ARCSEC`) to configuration section

## Dependencies & Risks

| Risk | Mitigation |
|------|-----------|
| `lsdb.open_catalog()` S3 access fails in containers | Pass `storage_options={'anon': True}`; add `s3fs` to requirements |
| `from_dataframe()` slow at 100k rows | Docs say "under 1M rows"; prototype to confirm |
| Missing matches near tile boundaries | Verify Gaia DR3 HATS ships with margin cache; at 1 arcsec, effect is small |
| LSDB API changes | Pin `lsdb` version in requirements |
| Gaia HATS bucket URL changes | Configurable via `GAIA_HATS_URL` env var |
| Worker OOM kill leaves alerts stuck in QUEUED | Follow-up: add stale-QUEUED recovery mechanism |
| NaN coordinates in alert batch | Filter with `dropna()` before `from_dataframe()` |

## Success Metrics

- All HEROIC code, config, and references removed (zero grep hits)
- Crossmatch task produces real CatalogMatch rows from Gaia DR3
- Unmatched alerts transition to MATCHED without CatalogMatch rows (no stuck QUEUED alerts)
- Batch of 100k alerts processes within acceptable time (< 5 min target)
- No new external service dependencies (S3 is passive storage)

## Deferred Items

- **Zombie QUEUED recovery** — periodic cleanup of stale QUEUED alerts after worker crashes. Not needed in initial dev but required before production.
- **`CrossmatchRun` model integration** — model exists but has per-alert FK; needs schema redesign for per-batch tracking before use.
- **`CROSSMATCH_N_NEIGHBORS` setting** — hardcoded to 1 for now. Add configurable setting only when a science use case for multiple neighbors emerges.
- **Stale periodic task cleanup** — in production deployments, old "Query HEROIC" PeriodicTask rows would need cleanup. Not applicable in current dev stage (no persistent DB state).

## References & Research

### Internal References

- Brainstorm: `docs/brainstorms/2026-03-10-healpix-crossmatch-refactor-brainstorm.md`
- Design argument: `healpix_vs_visit_crossmatch.md`
- Pseudocode (superseded): `pseudo-code-healpix-cell-grouping.py`
- Alert model: `crossmatch/core/models.py:8-52`
- CatalogMatch model: `crossmatch/core/models.py:105-138`
- Crossmatch task stub: `crossmatch/tasks/crossmatch.py`
- Gaia matching stub: `crossmatch/matching/gaia.py`
- Batch dispatcher: `crossmatch/tasks/schedule.py:43-99`
- Settings: `crossmatch/project/settings.py`
- Docker config: `docker/docker-compose.yaml`
- Helm values: `kubernetes/charts/crossmatch-service/values.yaml`

### LSDB API (from documentation research)

- `lsdb.from_dataframe()` — adaptive HEALPix partitioning (orders 0-7); raises `ValueError` on NaN coords
- `lsdb.open_catalog(path, storage_options={'anon': True})` — loads HATS catalogs (NOT `read_hats()`); use `columns=` to limit I/O
- `catalog.crossmatch(other, suffixes=('_alert', '_gaia'))` — KDTreeCrossmatch, returns `_dist_arcsec` column
- Gaia DR3 HATS: `s3://stpubdata/hats/gaia/dr3/` (public, `anon=True` required)
- Catalog object is lightweight (metadata only) — safe to cache as module-level singleton

### Files to Delete

- `crossmatch/heroic/client.py`
- `crossmatch/heroic/schedule_sync.py`
- `crossmatch/heroic/__init__.py`
- `crossmatch/matching/constraints.py`
- `crossmatch/project/management/commands/sync_pointings.py`
- `pseudo-code-healpix-cell-grouping.py`

### Files to Modify

- `crossmatch/core/models.py` — remove PlannedPointing
- `crossmatch/tasks/schedule.py` — remove HEROIC tasks, keep DispatchCrossmatchBatch
- `crossmatch/tasks/crossmatch.py` — replace stub with real LSDB crossmatch
- `crossmatch/matching/gaia.py` — implement real crossmatch function
- `crossmatch/project/settings.py` — remove HEROIC setting, add LSDB settings
- `crossmatch/tasks/schedule.py:dispatch_crossmatch_batch` — pass `batch_ids` to task
- `docker/docker-compose.yaml` — remove HEROIC env vars, add LSDB env vars
- `kubernetes/charts/crossmatch-service/values.yaml` — add crossmatch settings
- `crossmatch/requirements.base.txt` — add `s3fs`
- `scimma_crossmatch_service_design.md` — extensive updates
