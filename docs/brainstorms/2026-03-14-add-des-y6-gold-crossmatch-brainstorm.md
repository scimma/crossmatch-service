---
topic: Add DES Y6 Gold catalog crossmatch with configurable catalog list
date: 2026-03-14
status: complete
---

# Add DES Y6 Gold Catalog Crossmatch

## Context

The crossmatch service currently matches alerts against a single catalog
(Gaia DR3). The `CatalogMatch` model already supports multiple catalogs via
a `catalog_name` discriminator field, but the matching code (`matching/gaia.py`)
and task (`crossmatch_batch`) are hardcoded to Gaia.

The DES Y6 Gold catalog is available via LSDB at
`https://data.lsdb.io/hats/des/des_y6_gold` and should be added as a second
crossmatch target. The design document (§7.4) lists DES among planned future
catalogs.

## What We're Building

A configurable catalog registry in Django settings that defines which LSDB
catalogs to crossmatch against. The `crossmatch_batch` task loops through
all configured catalogs sequentially, running each crossmatch and creating
`CatalogMatch` and `Notification` rows per catalog. The existing Gaia
matching becomes one entry in the registry, and DES Y6 Gold is added as a
second entry.

## Why This Approach

### Chosen: Configurable catalog list in Django settings

**Over "always both" hardcoding:** A configurable list lets operators
enable/disable catalogs per environment without code changes. Dev
environments can run with just one catalog for speed; production can run
all catalogs.

**Over YAML config file:** Django settings with env vars is consistent with
how `GAIA_HATS_URL` and all other service configuration works today. Adding
a separate config file format would be inconsistent.

**Over database table:** A Django model for catalog config adds migration
complexity and runtime DB lookups for something that changes rarely. Settings
are simpler and sufficient.

### Chosen: Sequential crossmatch in same task

**Over per-catalog sub-tasks:** Sequential execution in one task avoids
orchestration complexity (fan-out/join, partial failure tracking, alert
status transitions across multiple tasks). The crossmatch_batch task already
holds the QUEUED lock on alerts — splitting into sub-tasks would complicate
the concurrency model. Sequential is simpler and sufficient for 2-3 catalogs.

## Key Decisions

1. **Catalog configuration in Django settings** — a list of catalog
   definitions, each with: `name` (used in `CatalogMatch.catalog_name`),
   `hats_url` (LSDB HATS URL), and `source_id_column` (column name for the
   catalog's primary ID). Defined as a Python list in `settings.py` with
   URLs sourced from env vars (e.g., `GAIA_HATS_URL`, `DES_HATS_URL`).
   The list structure lives in code; env vars control the URLs.

2. **Global match radius** — one `CROSSMATCH_RADIUS_ARCSEC` for all
   catalogs. Per-catalog radius is YAGNI for now.

3. **Sequential execution** — `crossmatch_batch` loops through configured
   catalogs, crossmatches each one, and creates `CatalogMatch` rows. All
   catalogs processed in the same task invocation.

4. **One Notification per CatalogMatch** — each catalog match generates
   its own `Notification` row with that catalog's match data. Consistent
   with the current per-match notification pattern.

5. **Minimal payload for DES** — `diaObjectId`, `ra`, `dec`,
   `catalog_source_id` (COADD_OBJECT_ID), `separation_arcsec`. Same
   pattern as Gaia payload. Additional columns can be added later.

6. **Generic matching function** — replace `matching/gaia.py` with a
   generic function that accepts catalog config (URL, source ID column)
   and returns results. Assumes all LSDB HATS catalogs have standard
   `ra`/`dec` columns — only the source ID column name varies per catalog.
   The Gaia-specific caching pattern (module-level singleton) extends to
   a dict keyed by catalog name.

7. **DES Y6 Gold LSDB URL** —
   `https://data.lsdb.io/hats/des/des_y6_gold`, opened with
   `lsdb.open_catalog(url)`. Source ID column is `COADD_OBJECT_ID`.

## Open Questions

None — all decisions resolved during brainstorming.
