---
title: Replace HEROIC Pointing Constraints with LSDB Native Crossmatching
type: refactor
date: 2026-03-10
---

# Replace HEROIC Pointing Constraints with LSDB Native Crossmatching

## What We're Building

A refactoring of the crossmatch service that removes HEROIC telescope pointing data from the crossmatch pipeline entirely. Instead of using HEROIC pointings to constrain spatial queries, we rely on LSDB's native `from_dataframe()` + `crossmatch()` API, which handles HEALPix partitioning, spatial alignment, and catalog matching internally.

## Why This Approach

The original design assumed HEROIC planned pointings (center + FOV radius) would constrain LSDB spatial queries during crossmatching. After analysis, this is unnecessary for several reasons:

1. **Alerts have precise coordinates** — visit pointings only give a ~3.5 degree field center, far less precise than the alert ra/dec themselves.
2. **HATS catalogs use adaptive HEALPix partitioning** — LSDB handles partition alignment automatically when crossmatching. Manual HEALPix grouping with healpy adds complexity without benefit.
3. **Visit footprints are too large** — a single Rubin visit (~9.6 sq deg) spans dozens of HATS partitions, loading many tiles with no alerts.
4. **Alerts span multiple visits** — a batch of 100k alerts may come from multiple visits, nights, and filters.
5. **No external dependency** — eliminates the HEROIC API dependency, periodic sync task, and associated failure modes.
6. **LSDB does it better** — `lsdb.from_dataframe()` converts a pandas DataFrame into an LSDB Catalog with adaptive HEALPix partitioning (orders 0-7), then `catalog.crossmatch()` handles spatial alignment and matching. No manual HEALPix cell grouping needed.

See `healpix_vs_visit_crossmatch.md` for the full argument against visit-based constraints.

## Key Decisions

1. **Remove HEROIC entirely** — delete all HEROIC code (client, schedule_sync, periodic task), the PlannedPointing model/table/migration, env vars, Helm chart config, docker-compose config, and all references in the design document. Clean break.

2. **Use LSDB `from_dataframe()` + `crossmatch()` directly** — no manual HEALPix cell grouping with healpy. LSDB handles adaptive partitioning internally. The crossmatch flow becomes:
   ```python
   alerts_catalog = lsdb.from_dataframe(df, ra_column='ra', dec_column='dec')
   gaia = lsdb.read_hats("https://data.lsdb.io/hats/gaia_dr3")
   matches = alerts_catalog.crossmatch(gaia, n_neighbors=1, radius_arcsec=1.0)
   result = matches.compute()
   ```

3. **No healpy dependency needed** — since LSDB handles HEALPix partitioning internally via `from_dataframe()`, we don't need healpy as a direct dependency. LSDB's adaptive partitioning (orders 0-7) is better than a fixed NSIDE anyway.

4. **Default crossmatch radius: 1 arcsec, configurable** — accounts for LSST position uncertainty (~10-20 mas) plus Gaia proper motion offsets. Configurable via env var `CROSSMATCH_RADIUS_ARCSEC`.

5. **Batch dispatch unchanged** — the existing batch dispatcher continues to grab up to N alerts by time/count thresholds. The `crossmatch_batch` task loads QUEUED alerts into a DataFrame and uses LSDB directly. Start with single-task execution; optimize to parallel fan-out later only if performance requires it (YAGNI).

6. **Full cleanup scope** — remove all HEROIC references from design doc, code layout, open questions, env vars, Helm chart, and docker-compose. Add LSDB-native crossmatch strategy throughout.

## Crossmatch Flow (New)

```
1. Batch dispatcher (every 30s) grabs up to BATCH_MAX_SIZE INGESTED alerts
2. Transitions them to QUEUED, enqueues crossmatch_batch task
3. crossmatch_batch loads QUEUED alerts (id, ra, dec) into pandas DataFrame
4. Converts to LSDB catalog: lsdb.from_dataframe(df)
5. Loads Gaia HATS catalog: lsdb.read_hats(GAIA_HATS_URL)
6. Crossmatches: alerts_catalog.crossmatch(gaia, n_neighbors=1, radius_arcsec=1.0)
7. Computes results, writes to catalog_matches table
8. Transitions alerts to MATCHED
```

## What Gets Removed

- `crossmatch/heroic/` package (client.py, schedule_sync.py, __init__.py)
- `core/models.py` PlannedPointing model
- `core/migrations/0001_initial.py` PlannedPointing table creation
- `tasks/schedule.py` RefreshPlannedPointings class and task
- `matching/constraints.py` pointings_covering() stub
- `project/management/commands/sync_pointings.py`
- `project/management/commands/initialize_periodic_tasks.py` stale task cleanup for "Query HEROIC"
- `entrypoints/django_init.sh` sync_pointings call
- `project/settings.py` HEROIC_BASE_URL, QUERY_HEROIC_INTERVAL
- `docker-compose.yaml` HEROIC_BASE_URL, QUERY_HEROIC_INTERVAL env vars
- `kubernetes/` Helm chart heroic values section and HEROIC env var templates
- Design document sections: §4.3, §5.2.2, §11.1, HEROIC references in §6, §8.4, §9, §10

## What Gets Added

- `CROSSMATCH_RADIUS_ARCSEC` env var / Django setting (default 1.0)
- `CROSSMATCH_N_NEIGHBORS` env var / Django setting (default 1)
- `GAIA_HATS_URL` env var / Django setting (default `s3://stpubdata/hats/gaia/dr3/`)
- Updated `matching/gaia.py` — implement crossmatch using `lsdb.from_dataframe()` + `catalog.crossmatch()`
- Updated `tasks/crossmatch.py` — call the real matching logic instead of stub
- Updated design document with LSDB-native crossmatch strategy replacing HEROIC throughout

## LSDB API Notes (from documentation research)

### `lsdb.from_dataframe(df, ra_column, dec_column, ...)`
- Converts pandas DataFrame to LSDB Catalog with adaptive HEALPix partitioning
- `lowest_order=0`, `highest_order=7` by default (adaptive, not fixed NSIDE)
- Best for datasets under 1M rows — fits our batch size of 100k
- Auto-detects "ra"/"dec" column names (case-insensitive)

### `catalog.crossmatch(other, n_neighbors=1, radius_arcsec=1.0)`
- Returns a Catalog with merged columns from both inputs
- Default algorithm: KDTreeCrossmatch
- `how='inner'` by default (only matched rows)
- Handles partition alignment internally
- Returns distance measurements in result columns

### HATS Adaptive Partitioning
- HATS does NOT use a fixed NSIDE/order — it adaptively sizes partitions so each has roughly the same number of objects
- This means our initial plan to use a fixed healpy NSIDE=64 was misaligned with how HATS actually works
- `lsdb.from_dataframe()` does the right thing automatically

## Resolved Open Questions

1. **~~What NSIDE does Gaia DR3 HATS use?~~** — HATS uses adaptive partitioning, not a fixed NSIDE. `lsdb.from_dataframe()` handles alignment automatically. No need to match catalog NSIDE.

2. **Match radius** — 1 arcsec default, configurable via `CROSSMATCH_RADIUS_ARCSEC`. Conservative enough for LSST position uncertainty + proper motion.

3. **~~healpy vs LSDB~~** — LSDB's `from_dataframe()` handles HEALPix partitioning natively. No healpy dependency needed.

4. **Parallel fan-out** — deferred. Start with single-task sequential execution. LSDB uses Dask internally for parallelism. Optimize only if 100k-alert batches prove too slow.

5. **Gaia HATS catalog via S3** — use `s3://stpubdata/hats/gaia/dr3/` (public bucket, no credentials needed). Configurable via `GAIA_HATS_URL` env var.

6. **Gaia columns: minimal** — store only `source_id`, `ra`, `dec`, and match distance in `catalog_payload`. Consumers can query Gaia directly for additional fields if needed.

7. **`n_neighbors` configurable, default 1** — `CROSSMATCH_N_NEIGHBORS` env var. Nearest match only by default; can be increased for science use cases requiring multiple matches.

8. **Margin cache: deferred** — at 1 arcsec radius, edge effects are negligible. Investigate only if testing reveals missing matches near tile boundaries.

## Remaining Open Questions

1. **`from_dataframe()` performance at 100k rows** — docs say "best for datasets under 1M rows", but should prototype to confirm latency is acceptable.
