---
title: "docs: Recover §7.4 additional catalogs and generalize gaia_matches to catalog_matches"
type: docs
date: 2026-03-06
brainstorm: docs/brainstorms/2026-03-06-multi-catalog-matches-brainstorm.md
---

# docs: Recover §7.4 Additional Catalogs and Generalize `gaia_matches` → `catalog_matches`

## Overview

Two coordinated updates to `scimma_crossmatch_service_design.md`:

1. **Recover §7.4** — restore the "Planned Expansion to Additional Catalogs" section
   (DES, DELVE, SkyMapper, Pan-STARRS1) that was lost in an earlier content update.

2. **Generalize §5.2.3** — rename `gaia_matches` → `catalog_matches` and redesign
   the table to support crossmatching against multiple catalogs using a single unified
   table with a `catalog_name` discriminator column.

Design-document changes only. Django model and migration changes are a separate task.

---

## Acceptance Criteria

### §7.4 restoration
- [x] Section `### 7.4 Planned Expansion to Additional Catalogs` inserted between §7.3 and `## 8`
- [x] Lists DES, DELVE, SkyMapper, Pan-STARRS1 as planned catalogs
- [x] Motivation: deeper photometric coverage, improved counterpart ID, richer annotation
- [x] Notes architectural readiness: LSDB uniform interface, Celery tasks, `catalog_matches` table
- [x] Lists anticipated changes when adding new catalogs

### §5.2.3 generalization
- [x] Section header renamed: `gaia_matches` → `catalog_matches`
- [x] `gaia_source_id BIGINT` → `catalog_source_id TEXT NOT NULL`
- [x] `catalog_name TEXT NOT NULL` column added
- [x] `gaia_ra_deg` → `source_ra_deg`; `gaia_dec_deg` → `source_dec_deg`
- [x] `gaia_payload` → `catalog_payload`
- [x] UNIQUE constraint updated: `(lsst_diaObject_diaObjectId, catalog_name, catalog_source_id, match_version)`
- [x] `INDEX(catalog_name)` added

### §5.2.5 notifications FK
- [x] `gaia_match_id BIGINT NULL REFERENCES gaia_matches(id)` → `catalog_match_id BIGINT NULL REFERENCES catalog_matches(id)`

### Other references
- [x] Sequence diagram `UPSERT gaia_matches` → `UPSERT catalog_matches`
- [x] Sequence diagram `Write results to gaia_matches` → `Write results to catalog_matches`
- [x] §10 open question #3 `gaia_payload` → `catalog_payload`

---

## References

- Brainstorm: `docs/brainstorms/2026-03-06-multi-catalog-matches-brainstorm.md`
- Design document: `scimma_crossmatch_service_design.md`
- Source for §7.4 content: `~/Downloads/scimma_crossmatch_service_design.md`
