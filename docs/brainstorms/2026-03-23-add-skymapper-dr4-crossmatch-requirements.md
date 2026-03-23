---
date: 2026-03-23
topic: add-skymapper-dr4-crossmatch
---

# Add SkyMapper DR4 Crossmatch

## Problem Frame

The crossmatch-service currently matches alerts against Gaia DR3, DES Y6 Gold, and DELVE DR3 Gold. SkyMapper DR4 is a southern-sky survey catalog available as a HATS catalog at `https://data.lsdb.io/hats/skymapper_dr4/catalog`. Adding it extends counterpart coverage.

## Requirements

- R1. Add SkyMapper DR4 to the `CROSSMATCH_CATALOGS` registry with `name: 'skymapper_dr4'`, `hats_url` from `SKYMAPPER_HATS_URL` env var, `source_id_column: 'object_id'`, `ra_column: 'raj2000'`, `dec_column: 'dej2000'`
- R2. Add `SKYMAPPER_HATS_URL` env var (default: `https://data.lsdb.io/hats/skymapper_dr4/catalog`) to settings, docker-compose, .env.example, Helm helpers, values, and dev-overrides example
- R3. Update the design document §7.3 to list SkyMapper DR4 as an active catalog

## Success Criteria

- Alerts within SkyMapper DR4 footprint produce `CatalogMatch` rows with `catalog_name='skymapper_dr4'`
- Per-catalog error isolation: failures don't affect other catalog crossmatches

## Scope Boundaries

- Config-only change — no code modifications
- Same crossmatch radius (1 arcsec) applies

## Key Decisions

- Column names confirmed via `lsdb.open_catalog()`: `object_id`, `raj2000`, `dej2000` — different convention from Gaia/DES/DELVE but handled by the per-catalog column config

## Next Steps

→ `/ce:plan` for structured implementation planning
