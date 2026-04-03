---
date: 2026-04-03
topic: handle-no-overlap-as-no-match
---

# Handle "Catalogs do not overlap" as No-Match

## Problem Frame
When a batch of alerts has no spatial overlap with a reference catalog, LSDB raises `RuntimeError("Catalogs do not overlap")`. The current code treats this as an unexpected error — logging a full traceback at ERROR level. This is a normal operational condition (no matches found) and should be handled gracefully.

## Requirements
- R1. Catch `RuntimeError("Catalogs do not overlap")` from LSDB crossmatch and treat it as zero matches rather than an error.
- R2. Log the no-overlap condition at INFO level, consistent with the existing "No matches found" log message.
- R3. Continue processing remaining catalogs in the batch after a no-overlap result (current behavior, preserve it).

## Success Criteria
- No traceback logged when alerts don't overlap a catalog.
- INFO-level log message clearly indicates no overlap occurred, including the catalog name.
- Batch processing continues normally through all configured catalogs.

## Scope Boundaries
- Not changing LSDB behavior or the crossmatch radius.
- Not adding retry logic for this case (it's not transient).

## Next Steps
→ `/ce:plan` for structured implementation planning, or proceed directly to work given the lightweight scope.
