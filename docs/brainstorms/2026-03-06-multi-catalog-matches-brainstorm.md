---
date: 2026-03-06
topic: recover §7.4 additional catalogs section and generalize gaia_matches table
branch: main
---

# Brainstorm: Multi-Catalog Support — §7.4 Recovery and gaia_matches Generalization

## What We're Building

Two coordinated design document updates to `scimma_crossmatch_service_design.md`:

1. **Recover §7.4** — restore the "Planned Expansion to Additional Catalogs"
   section that was present in an earlier version of the document and dropped
   during a content update. Source: `~/Downloads/scimma_crossmatch_service_design.md`.

2. **Generalize §5.2.3 `gaia_matches`** — rename and redesign the table to
   support crossmatching against multiple catalogs in a single, unified table.
   Also update the `notifications` table FK that references it.

These are design document changes only. Django model and migration changes are
a separate future task.

---

## Key Decisions

### 1. Recovered §7.4 Content

The missing section is `### 7.4 Planned Expansion to Additional Catalogs`.
It appears between `§7.3 Match policy` and `§8 Python Implementation`.

**Content to restore (verbatim from earlier version, with light editing):**

After the Gaia crossmatching workflow is fully operational and validated, the
system will extend to additional large-area survey catalogs:

- **Dark Energy Survey (DES)**
- **DECam Local Volume Exploration Survey (DELVE)**
- **SkyMapper**
- **Pan-STARRS1 (PS1)**

Motivation:
- Deeper and complementary photometric coverage beyond Gaia.
- Improved counterpart identification for faint extragalactic or transient sources.
- Richer annotation of LSST alerts with multi-survey context.

The architectural design already supports this:
- LSDB provides a uniform interface for HATS-formatted catalogs.
- Crossmatch logic is encapsulated in Celery tasks and matching modules.
- The `catalog_matches` table (see §5.2.3) uses a `catalog_name` column to
  store results from any catalog in a single table.

When additional catalogs are introduced, anticipated changes:
- Adding separate LSDB catalog URL configs (e.g., `DES_HATS_URL`, `PS1_HATS_URL`).
- Extending the matching layer to support per-catalog matching policies.
- Adding per-catalog env vars following the pattern `{CATALOG}_HATS_URL`.
- No changes to the core ingestion, queueing, or deployment architecture.

---

### 2. Table rename and generalization: `gaia_matches` → `catalog_matches`

**Strategy**: single `catalog_matches` table with a `catalog_name` column.
All catalog crossmatch results live in one table; the `catalog_name` field
(e.g., `'gaia_dr3'`, `'des_dr2'`, `'ps1_dr2'`) distinguishes the source.

**Column changes:**

| Old column | New column | Type change | Notes |
|---|---|---|---|
| *(table name)* | `catalog_matches` | — | rename |
| `gaia_source_id` | `catalog_source_id` | BIGINT → **TEXT** | Different catalogs use different ID formats; TEXT is universal |
| `gaia_ra_deg` | `source_ra_deg` | — | generic name |
| `gaia_dec_deg` | `source_dec_deg` | — | generic name |
| `gaia_payload` | `catalog_payload` | — | generic name |
| *(new)* | `catalog_name` | TEXT NOT NULL | e.g., `'gaia_dr3'`, `'des_dr2'` |

**Full updated table schema:**

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaObject_diaObjectId | TEXT NOT NULL REFERENCES alerts(lsst_diaObject_diaObjectId) | |
| catalog_name | TEXT NOT NULL | e.g., `'gaia_dr3'`, `'des_dr2'`, `'ps1_dr2'` |
| catalog_source_id | TEXT NOT NULL | Source identifier in the named catalog |
| match_distance_arcsec | DOUBLE PRECISION NOT NULL | angular separation |
| match_score | DOUBLE PRECISION NULL | optional scoring |
| source_ra_deg | DOUBLE PRECISION NULL | cached source position (optional) |
| source_dec_deg | DOUBLE PRECISION NULL | |
| catalog_payload | JSONB NULL | catalog-specific columns (e.g., parallax, mag) |
| match_version | INTEGER NOT NULL DEFAULT 1 | algorithm versioning |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

**UNIQUE constraint** (updated to include catalog_name):
```
UNIQUE(lsst_diaObject_diaObjectId, catalog_name, catalog_source_id, match_version)
```

**Indexes** (unchanged in purpose):
- `INDEX(lsst_diaObject_diaObjectId)`
- `INDEX(catalog_name)` — new; supports filtering by catalog
- `INDEX(catalog_source_id)`

**Rationale for `catalog_source_id` as TEXT**: Gaia uses 64-bit integer IDs
(`source_id`), but DES uses `coadd_object_id` (also numeric), while PS1 uses
`objID` (numeric) and SkyMapper uses `object_id` (numeric). All can be stored
as TEXT without loss. The BIGINT constraint in `gaia_matches` would prevent
non-Gaia catalogs from using the column cleanly.

---

### 3. `notifications` table FK update

The `notifications` table currently has:
```
gaia_match_id | BIGINT NULL REFERENCES gaia_matches(id)
```

Update to:
```
catalog_match_id | BIGINT NULL REFERENCES catalog_matches(id)
```

The FK type stays BIGINT (referencing `catalog_matches.id` which is BIGSERIAL).
Only the column name and the referenced table name change.

---

## Changes Required in the Design Document

| Section | Change |
|---|---|
| §5.2.3 | Rename `gaia_matches` → `catalog_matches`; add `catalog_name`; rename source/payload columns; update UNIQUE constraint; add `INDEX(catalog_name)` |
| §5.2.5 | `notifications.gaia_match_id` → `catalog_match_id`; update FK reference |
| §7.4 | Restore recovered section (DES, DELVE, SkyMapper, PS1 expansion plan) |
| §8.4 Celery tasks | Update task name reference if it mentions `gaia_matches` |
| §11.2 Milestone | Note is about `gaia_matches` → update to `catalog_matches` |

---

## Open Questions

- **`catalog_name` values**: should we define a controlled vocabulary now
  (e.g., `'gaia_dr3'`, `'des_dr2'`) or leave it as free text? An enum or
  check constraint could enforce consistency.
- **HATS availability**: DES, DELVE, SkyMapper, and PS1 HATS catalogs should
  be confirmed as available in the LSST HATS ecosystem before being listed as
  planned targets. Are all four confirmed available?
- **`crossmatch_runs` table**: should `crossmatch_runs` also gain a
  `catalog_name` column to track per-catalog run state, or is `match_version`
  sufficient to distinguish Gaia vs. other catalog runs?
- **Django model and migration**: the `GaiaMatch` model in `core/models.py`
  will need renaming and new fields. This is tracked as a follow-on code task,
  not part of this design doc update.
