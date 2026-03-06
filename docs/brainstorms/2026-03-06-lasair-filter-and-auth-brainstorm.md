---
date: 2026-03-06
topic: Lasair filter criteria and Kafka authentication details
branch: main
---

# Brainstorm: Lasair Filter Criteria and Kafka Authentication

## What We're Building

Design document updates to close two open questions in
`scimma_crossmatch_service_design.md`:

1. **Kafka authentication** (§4.5, §9.1.3, §10 question #6): confirm that
   `lasair_consumer` requires no credentials for `kafka.lsst.ac.uk:9092`.
2. **Lasair filter criteria** (§2.1 B2, §4.5, §10 question #7): document the
   actual filter SQL, the `latestR` ML score, and the concrete Kafka topic name.

These are documentation-only changes derived from first-hand Lasair account
exploration and online research. No code changes are implied.

---

## Key Decisions

### 1. Kafka authentication: none required

`lasair_consumer` connects to `kafka.lsst.ac.uk:9092` without any credentials.
No SASL username/password and no bearer token are passed to the Kafka consumer.

**Implications for the design document:**
- §4.5 Authentication block: replace "TBD — confirm before implementation"
  with a clear statement that no credentials are needed.
- §9.1.3 Secrets: remove `LASAIR_TOKEN` from the secrets list for the
  `lasair-ingest` deployment. The REST API token (`lasair_client(token=...)`)
  is not used by the ingest path and should be noted as REST-API-only if it
  appears at all.
- §10 Open Question #6: mark as resolved.

### 2. Lasair filter: ML Real/Bogus score + recency window

The filter has been created on the Lasair web UI. The complete SQL is:

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

**Field explanations:**

| Field | Description |
|---|---|
| `diaObjectId` | Lasair's identifier for the LSST DIA object — maps to `lsst_diaObject_diaObjectId` |
| `ra`, `decl` | LSST positional fields (degrees) |
| `nDiaSources` | Number of individual DIA detections linked to this object |
| `lastDiaSourceMjdTai` | MJD of the most recent detection |
| `latestR` | Lasair Real/Bogus ML score for the latest source (0–1; 1 = real transient) |
| `age` | Computed: days since last detection (not stored; for display only) |

**Filter criteria semantics:**

- `nDiaSources >= 1` — any object with at least one detection. This is a
  minimal gate; the `latestR` threshold does the real quality filtering.
- `latestR > 0.7` — the latest DIA source has a Real/Bogus score above 0.7.
  This is a Lasair-computed ML score that acts as a single proxy for the many
  individual artifact flags used by the ANTARES filter (no dipole, no streak,
  no saturated pixels, etc.). The filter name `reliability_moderate` reflects
  this threshold.
- `mjdnow() - lastDiaSourceMjdTai < 1` — the object had a detection within
  the last 24 hours. This ensures we only receive recent/active transients and
  avoids re-delivering old objects every time the Kafka server replays.

### 3. Kafka topic name

```
lasair_366SCiMMA_reliability_moderate
```

- `366` — Lasair user ID for the SCiMMA account.
- `SCiMMA` — organization tag in the filter name.
- `reliability_moderate` — descriptive name for the `latestR > 0.7` threshold.

This is the value for `LASAIR_TOPIC` in all environments (development and
production). In development, set a throwaway `LASAIR_GROUP_ID` to replay
cached alerts.

### 4. Filter philosophy: ML-based vs. flag-based

The Lasair and ANTARES filters take different approaches to artifact rejection:

| Aspect | ANTARES filter | Lasair filter |
|---|---|---|
| Artifact rejection | Explicit boolean flags (dipole, streak, saturation, edge, CR) | ML Real/Bogus score (`latestR > 0.7`) |
| SNR requirement | `lsst_diaSource_snr > 10` | None explicit (ML score covers this implicitly) |
| Solar System exclusion | `ssObjectId in (0, None)` | None (not a Lasair filter field) |
| Recency | Not explicit | Last detection within 24 hours |
| Minimum detections | Not explicit | `nDiaSources >= 1` |

This means the two filters are complementary rather than equivalent. The Lasair
filter may pass objects that ANTARES rejects (e.g., low-SNR real transients
that score > 0.7 on Real/Bogus) and vice versa (e.g., high-SNR objects near
the image edge). Both paths write to the same `alerts` table; the
deduplication UPSERT ensures each unique `diaObjectId` is crossmatched once
regardless of which broker delivers it first.

---

## Changes Required in the Design Document

| Section | Change |
|---|---|
| §2.1 B2 Lasair Ingest | Replace "Lasair filter criteria (TBD)" with the actual SQL and field explanations |
| §4.5 Authentication | Replace "TBD — confirm before implementation" with "No credentials required" |
| §4.5 env vars table | Add `LASAIR_TOPIC=lasair_366SCiMMA_reliability_moderate` as concrete example |
| §9.1.3 Secrets | Remove `LASAIR_TOKEN` from `lasair-ingest` secrets (not needed for Kafka ingest) |
| §10 Open Questions | Mark #6 (Kafka auth) and #7 (filter/topic) as resolved |

---

## Open Questions (Remaining)

- **Lasair alert JSON schema**: the filter SELECT defines what columns the
  streaming message contains (`diaObjectId`, `ra`, `decl`, `nDiaSources`,
  `age`). Is this the complete Kafka message payload, or does the message
  include additional LSST alert packet fields? Confirm by consuming a test
  message.
- **`raw_payload` contents**: given the filter output, `alert_deliveries.raw_payload`
  for Lasair should capture at minimum `nDiaSources` and `latestR`. We should
  confirm whether `latestR` appears in the streamed message or only in the
  filter's WHERE clause (not in the SELECT).
- **Solar System exclusion**: the ANTARES filter explicitly excludes Solar
  System objects (`ssObjectId in (0, None)`). The Lasair filter does not.
  Is this intentional (rely on Real/Bogus to reject them), or should we add
  an `ssObjectId` filter condition if Lasair exposes that field?
- **`nDiaSources >= 1` permanence**: this is a very permissive gate. Should
  we raise it (e.g., `>= 2`) to reduce single-epoch false positives, or keep
  it minimal and let Real/Bogus do the work?
