---
date: 2026-03-23
topic: add-delve-dr3-gold-crossmatch
---

# Add DELVE DR3 Gold Crossmatch

## Problem Frame

The crossmatch-service currently matches alerts against Gaia DR3 and DES Y6 Gold. DELVE DR3 Gold is another large-area survey catalog available as a HATS catalog at `https://data.lsdb.io/hats/delve/delve_dr3_gold`. Adding it extends coverage for counterpart identification, particularly for faint sources in the southern sky.

## Requirements

- R1. Add DELVE DR3 Gold to the `CROSSMATCH_CATALOGS` registry in `settings.py` with `name: 'delve_dr3_gold'`, `hats_url` from `DELVE_HATS_URL` env var, `source_id_column: 'COADD_OBJECT_ID'`, `ra_column: 'RA'`, `dec_column: 'DEC'`
- R2. Add `DELVE_HATS_URL` env var (default: `https://data.lsdb.io/hats/delve/delve_dr3_gold`) to `settings.py`, `docker-compose.yaml`, `.env.example`, Helm `_helpers.yaml`, and `values.yaml`
- R3. Update the design document §7.3 to list DELVE DR3 Gold as an active catalog (not planned)

## Success Criteria

- Alerts that fall within the DELVE DR3 Gold footprint produce `CatalogMatch` rows with `catalog_name='delve_dr3_gold'`
- Alerts outside the footprint log "No matches found" or "Catalogs do not overlap" without affecting other catalog crossmatches (per-catalog error isolation)

## Scope Boundaries

- No code changes to `matching/catalog.py` or `tasks/crossmatch.py` — the configurable registry handles this
- No new database models or migrations
- No changes to notification payload structure (already uses generic `catalog_name` + `catalog_source_id`)

## Key Decisions

- Column names confirmed via `lsdb.open_catalog()`: `COADD_OBJECT_ID`, `RA`, `DEC` (same convention as DES Y6 Gold)
- Same crossmatch radius (1 arcsec) applies to all catalogs

## Next Steps

→ `/ce:plan` for structured implementation planning
