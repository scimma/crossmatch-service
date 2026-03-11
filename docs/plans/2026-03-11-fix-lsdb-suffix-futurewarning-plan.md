---
title: "Fix LSDB crossmatch suffix FutureWarning"
type: fix
date: 2026-03-11
---

# Fix LSDB crossmatch suffix FutureWarning

LSDB 0.8.2 emits a FutureWarning on every `crossmatch()` call: the default suffix
behavior will change from applying suffixes to all columns to only overlapping columns.
Adopt the future default now by passing `suffix_method='overlapping_columns'` and
updating column references in the result DataFrame.

## Acceptance Criteria

- [x] Add `suffix_method='overlapping_columns'` to `crossmatch()` call in `crossmatch/matching/gaia.py`
- [x] Update column references in `crossmatch/tasks/crossmatch.py`:
  - `lsst_diaObject_diaObjectId_alert` → `lsst_diaObject_diaObjectId`
  - `source_id_gaia` → `source_id`
  - `ra_gaia` stays `ra_gaia` (overlapping column)
  - `dec_gaia` stays `dec_gaia` (overlapping column)
- [ ] FutureWarning no longer appears in celery-worker logs (verify after deploy)
- [x] Update design document `scimma_crossmatch_service_design.md` crossmatch code example if it references suffixed columns

## References

- Brainstorm: `docs/brainstorms/2026-03-11-lsdb-suffix-futurewarning-brainstorm.md`
- `crossmatch/matching/gaia.py:48` — crossmatch() call
- `crossmatch/tasks/crossmatch.py:45-53` — result DataFrame column access
