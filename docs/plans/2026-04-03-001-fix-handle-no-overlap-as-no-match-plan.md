---
title: "fix: Handle LSDB no-overlap RuntimeError as normal no-match"
type: fix
status: completed
date: 2026-04-03
origin: docs/brainstorms/2026-04-03-handle-no-overlap-as-no-match-requirements.md
---

# fix: Handle LSDB no-overlap RuntimeError as normal no-match

## Overview

When a batch of alerts has no spatial overlap with a reference catalog, LSDB raises `RuntimeError("Catalogs do not overlap")`. The current generic `except Exception` handler logs this at ERROR level with a full traceback. This is a normal operational condition — not an error — and should be handled gracefully at INFO level (see origin: `docs/brainstorms/2026-04-03-handle-no-overlap-as-no-match-requirements.md`).

## Proposed Solution

Add a specific `RuntimeError` catch before the generic `except Exception` in the per-catalog loop of `crossmatch_batch()`. Check the error message for the LSDB-specific string, log at INFO, and continue to the next catalog. Re-raise non-matching RuntimeErrors so they still hit the generic handler.

## Change: `crossmatch/tasks/crossmatch.py`

Lines 62–67. Replace the current single `except Exception` block with a two-tier handler:

```python
# Current code:
try:
    result_df = crossmatch_alerts(alerts_catalog, catalog_config)
except Exception:
    logger.exception('Crossmatch failed for catalog',
                     catalog=catalog_name)
    continue

# New code:
try:
    result_df = crossmatch_alerts(alerts_catalog, catalog_config)
except RuntimeError as exc:
    if "Catalogs do not overlap" in str(exc):
        logger.info('No spatial overlap with catalog',
                     catalog=catalog_name, total=len(clean_df))
        continue
    raise
except Exception:
    logger.exception('Crossmatch failed for catalog',
                     catalog=catalog_name)
    continue
```

No other files need modification.

## Technical Considerations

- **String matching fragility:** The substring check `"Catalogs do not overlap"` depends on LSDB's error message wording. LSDB is pinned to `0.8.1` in `requirements.base.txt`, so the message is stable for now. If LSDB is upgraded, a test will catch any wording change.
- **Placement in task vs matching module:** Keeping the catch in `crossmatch_batch()` (the task) rather than `crossmatch_alerts()` (the library wrapper) is correct — the task owns policy decisions about how to handle no-match conditions, while the wrapper surfaces LSDB behavior faithfully.
- **Other RuntimeErrors:** LSDB can raise RuntimeError for other conditions (e.g., incompatible schemas). The `raise` in the non-matching branch correctly lets those propagate to the generic `except Exception` handler.

## Acceptance Criteria

- [ ] No traceback logged when alerts don't spatially overlap a catalog
- [ ] INFO-level structured log message with `catalog=<name>` and `total=<count>` keys
- [ ] Batch processing continues normally through remaining catalogs after a no-overlap result
- [ ] Other RuntimeErrors from LSDB still logged at ERROR level with traceback

## Sources

- **Origin document:** [docs/brainstorms/2026-04-03-handle-no-overlap-as-no-match-requirements.md](docs/brainstorms/2026-04-03-handle-no-overlap-as-no-match-requirements.md) — R1 (catch specific error), R2 (INFO level), R3 (continue processing)
- Error source: `lsdb/dask/merge_catalog_functions.py:527` in LSDB 0.8.1
- Current handler: `crossmatch/tasks/crossmatch.py:62-67`
