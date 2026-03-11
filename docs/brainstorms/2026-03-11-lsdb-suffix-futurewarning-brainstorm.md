---
title: "Future-proof LSDB crossmatch suffix behavior"
type: improvement
date: 2026-03-11
---

# Future-proof LSDB crossmatch suffix behavior

## What We're Building

Update the `crossmatch()` call and result DataFrame column references to use
`suffix_method='overlapping_columns'`, adopting the future LSDB default now.
This silences the FutureWarning and prevents breakage when LSDB changes the default.

## Why This Approach

LSDB 0.8.2 warns:

> The default suffix behavior will change from applying suffixes to all columns
> to only applying suffixes to overlapping columns in a future release.

Currently suffixes are applied to ALL columns. We just fixed column references
to use suffixed names (`lsst_diaObject_diaObjectId_alert`, `source_id_gaia`).
When LSDB changes the default, those names would break again.

By setting `suffix_method='overlapping_columns'` now:
- Only truly overlapping columns (`ra`, `dec`) get suffixes (`ra_alert`, `ra_gaia`, etc.)
- Non-overlapping columns keep original names (`lsst_diaObject_diaObjectId`, `uuid`, `source_id`)
- Code is forward-compatible with the next LSDB release
- FutureWarning is silenced

## Key Decisions

- Use `suffix_method='overlapping_columns'` in `crossmatch()` call
- Update column references in `crossmatch.py` to match new naming
- Overlapping columns (ra, dec) keep suffixes: `ra_gaia`, `dec_gaia`
- Non-overlapping columns lose suffixes: `source_id`, `lsst_diaObject_diaObjectId`

## Column Name Changes

| Current (all_columns) | New (overlapping_columns) | Why |
|----------------------|--------------------------|-----|
| `lsst_diaObject_diaObjectId_alert` | `lsst_diaObject_diaObjectId` | Non-overlapping |
| `source_id_gaia` | `source_id` | Non-overlapping |
| `ra_gaia` | `ra_gaia` | Overlapping (both have `ra`) |
| `dec_gaia` | `dec_gaia` | Overlapping (both have `dec`) |
| `uuid_alert` | `uuid` | Non-overlapping |

## Open Questions

None.
