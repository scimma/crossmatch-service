---
title: "docs: Fill in Lasair filter criteria and resolve Kafka auth open question"
type: docs
date: 2026-03-06
brainstorm: docs/brainstorms/2026-03-06-lasair-filter-and-auth-brainstorm.md
---

# docs: Fill in Lasair filter Criteria and Resolve Kafka Auth Open Question

## Overview

Update `scimma_crossmatch_service_design.md` to close two open questions with
first-hand information from a live Lasair account:

1. **Kafka authentication** — `lasair_consumer` requires no credentials for
   `lasair-lsst-kafka.lsst.ac.uk:9092`. Replace all "TBD" auth text and remove
   `LASAIR_TOKEN` from the ingest deployment secrets.
2. **Lasair filter criteria** — A filter named `reliability_moderate` has been
   created on the Lasair web UI. Document the full SQL, field explanations,
   and the concrete Kafka topic name `lasair_366SCiMMA_reliability_moderate`.

No code changes. Documentation only.

---

## Proposed Changes

### 1. §2.1 B2 — Replace Lasair filter criteria placeholder

Replace the current "Lasair Filter Selection Criteria (Open Question)" stub
with the actual filter SQL and a field-by-field explanation table.

**Current text (to replace):**
```
#### Lasair Filter Selection Criteria (Open Question)
The Lasair filter is configured via the Lasair web UI and produces a named Kafka topic.
The filter criteria should mirror the ANTARES criteria ...
The exact Lasair filter definition is **TBD** ...
```

**Replacement:**

```markdown
#### Lasair Filter Selection Criteria

The Lasair filter `reliability_moderate` has been created on the Lasair web UI.
It produces the Kafka topic `lasair_366SCiMMA_reliability_moderate`.

**Filter SQL:**
```sql
SELECT objects.diaObjectId,
       objects.ra,
       objects.decl,
       objects.nDiaSources,
       mjdnow() - objects.lastDiaSourceMjdTai AS age
FROM objects
WHERE objects.nDiaSources >= 1
  AND objects.latestR > 0.7
  AND mjdnow() - objects.lastDiaSourceMjdTai < 1
```

**Field descriptions:**

| Field | Description |
|---|---|
| `diaObjectId` | LSST DIA object identifier — maps to `lsst_diaObject_diaObjectId` |
| `ra`, `decl` | LSST positional fields (degrees) |
| `nDiaSources` | Number of individual DIA detections linked to this object |
| `lastDiaSourceMjdTai` | MJD of most recent detection |
| `latestR` | Lasair Real/Bogus ML score for latest source (0–1; 1 = real) |
| `age` | Computed days since last detection (display only; not stored) |

**Filter criteria semantics:**
- `nDiaSources >= 1` — any object with at least one detection.
- `latestR > 0.7` — Lasair's ML Real/Bogus score above 0.7. Acts as a single
  proxy for the artifact flags used by the ANTARES filter (dipole, streak,
  saturation, edge, cosmic ray). The filter name `reliability_moderate`
  reflects this threshold.
- `lastDiaSourceMjdTai` within 1 day — only recent/active transients are
  delivered; avoids re-delivering old objects on Kafka replay.

**Comparison with ANTARES filter:**
The two filters are complementary rather than equivalent. ANTARES uses explicit
boolean flags plus an SNR threshold; Lasair uses a single ML score plus a
recency window. Both paths write to the same `alerts` table; the deduplication
UPSERT ensures each `diaObjectId` is crossmatched exactly once regardless of
which broker delivers it first.
```

---

### 2. §4.5 — Replace authentication TBD

**Current text:**
```
**Authentication**: The mechanism for `lasair_consumer` Kafka access is **TBD** — the
public documentation does not explicitly state whether SASL credentials are required.
The Lasair REST API uses a bearer token (`lasair_client(token=...)`), but the Kafka
consumer may be unauthenticated. **Confirm before implementation.**
```

**Replacement:**
```markdown
**Authentication**: `lasair_consumer` connects to `lasair-lsst-kafka.lsst.ac.uk:9092`
**without any credentials** — no SASL username/password and no bearer token are
required. The Lasair REST API uses a bearer token (`lasair_client(token=...)`),
but this is not used by the Kafka consumer and is not needed for the ingest path.
```

---

### 3. §4.5 env vars table — update `LASAIR_TOPIC` example

**Current row:**
```
| `LASAIR_TOPIC` | `lasair_42_high-snr-transients` | from Lasair web UI |
```

**Replacement:**
```
| `LASAIR_TOPIC` | `lasair_366SCiMMA_reliability_moderate` | created on Lasair web UI |
```

---

### 4. §9.1.3 — Remove `LASAIR_TOKEN` from ingest secrets

**Current secrets list includes:**
```
- `LASAIR_TOKEN` — Lasair REST API token (if required for Kafka auth; TBD)
```

**Remove this line entirely.** Since no credentials are needed for Kafka
consumption, `LASAIR_TOKEN` is not a secret for the `lasair-ingest` Deployment.
If the REST API is ever used (e.g., for querying historical data), that is a
separate concern not part of the ingest path.

---

### 5. §10 Open Questions — mark #6 and #7 as resolved

**Question #6** (Lasair Kafka auth):
```
6. **Lasair Kafka auth**: does `lasair_consumer` require SASL credentials? If so,
   what format?
```
Replace with:
```
6. ~~**Lasair Kafka auth**~~ — **Resolved**: `lasair_consumer` connects to
   `lasair-lsst-kafka.lsst.ac.uk:9092` without credentials. No SASL config or token needed.
```

**Question #7** (Lasair filter/topic):
```
7. **Lasair filter/topic**: what filter criteria should the Lasair filter implement?
   ...
```
Replace with:
```
7. ~~**Lasair filter/topic**~~ — **Resolved**: filter `reliability_moderate`
   created on Lasair web UI; topic `lasair_366SCiMMA_reliability_moderate`.
   Criteria: `latestR > 0.7` AND `nDiaSources >= 1` AND last detection within
   1 day. See §2.1 B2 for full SQL.
```

---

## Acceptance Criteria

- [x] §2.1 B2 "Lasair Filter Selection Criteria" contains full filter SQL with field table and ANTARES comparison note
- [x] §4.5 Authentication block states "no credentials required" with no remaining TBD
- [x] §4.5 env vars table shows `lasair_366SCiMMA_reliability_moderate` as the `LASAIR_TOPIC` example
- [x] §9.1.3 Secrets no longer lists `LASAIR_TOKEN` for the ingest deployment
- [x] §10 Open Questions #6 (auth) marked resolved
- [x] §10 Open Questions #7 (filter/topic) marked resolved

## Dependencies & Risks

- **Remaining open questions**: §10 questions #8 (alert JSON schema) and #9
  (annotations to store in `raw_payload`) are still unresolved and should be
  left as-is.
- **`latestR` field in Kafka payload**: the filter SQL includes `latestR` in
  the WHERE clause but not in the SELECT. It is unknown whether `latestR`
  appears in the Kafka message body. This is captured in the brainstorm as an
  open question but does not block this documentation update.

## References & Research

- Brainstorm: `docs/brainstorms/2026-03-06-lasair-filter-and-auth-brainstorm.md`
- Design document: `scimma_crossmatch_service_design.md`
- Lasair alert streams docs: https://lasair.readthedocs.io/en/main/core_functions/alert-streams.html
