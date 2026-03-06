---
date: 2026-03-06
topic: add Lasair as a second alert broker alongside ANTARES
branch: refactor/align-skeleton-to-design
---

# Brainstorm: Add Lasair Alert Broker

## What We're Building

An extension of the design that adds **Lasair** as a second LSST alert broker alongside
ANTARES. Both brokers receive the same underlying LSST alert stream and provide
independent filtering/annotation layers. Running both gives us resilience (if one broker
is unavailable, the other keeps alerts flowing) and richer science filtering (each broker
offers different classification tools).

The core pipeline — crossmatch, HEROIC, notifier — stays the same. The change is
upstream of the `alerts` table: two independent ingest paths instead of one.

## Current State of the Design

`scimma_crossmatch_service_design.md` is entirely ANTARES-centric:

- Title: "LSST Alert Matching Service Architecture (ANTARES + Gaia)"
- Component B is "ANTARES Client / Ingest Service"
- Only ANTARES filter criteria are defined (§2.1A)
- `alerts` table has no broker provenance column
- Package layout has `antares/` with no sibling for Lasair
- Sequence diagram has one ingest lane (ANTARES only)
- Section 4.1 covers only ANTARES→Ingest interface
- No Lasair topic/client/auth discussed anywhere

## Key Decisions

### 1. Motivation: resilience + richer filtering
Both brokers run simultaneously. If ANTARES is unavailable, Lasair continues delivering
alerts and vice versa. Lasair also provides different science-based filters/annotations
that ANTARES does not.

### 2. Deduplication: track both sources, crossmatch once
Because ANTARES and Lasair both receive the same underlying LSST events, the same
`lsst_diaObject_diaObjectId` can arrive from both. We:
- UPSERT into `alerts` keyed by `lsst_diaObject_diaObjectId` (first arrival wins for
  the canonical alert row).
- Record each broker delivery in a new `alert_deliveries` table (one row per
  broker-per-alert).
- Only enqueue a Celery crossmatch task the first time an alert is seen (i.e., when
  the UPSERT creates a new `alerts` row, not on subsequent deliveries).

### 3. Package layout: brokers/ namespace
Rename `antares/` → `brokers/antares/` and add `brokers/lasair/`. A shared
`brokers/normalize.py` extracts the common LSST fields (ra, dec, diaObjectId,
diaSourceId, event_time, payload) from whichever broker's envelope delivered the alert.

```
brokers/
  __init__.py
  normalize.py          # shared LSST field extraction
  antares/
    __init__.py
    ingest.py
    normalize.py        # ANTARES-specific field extraction / annotation handling
  lasair/
    __init__.py
    ingest.py
    normalize.py        # Lasair-specific field extraction / annotation handling
```

### 4. alert_deliveries table
A new table records each broker delivery separately:

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaObject_diaObjectId | TEXT NOT NULL REFERENCES alerts(lsst_diaObject_diaObjectId) | |
| broker | TEXT NOT NULL | 'antares' or 'lasair' |
| broker_alert_id | TEXT NULL | broker-specific alert/event id if available |
| delivered_at | TIMESTAMPTZ NOT NULL DEFAULT now() | first delivery time |
| raw_payload | JSONB NULL | broker-specific envelope/annotations (not the LSST payload, which lives in alerts.payload) |

Constraints:
- `UNIQUE(lsst_diaObject_diaObjectId, broker)` — one record per broker per alert;
  re-deliveries from the same broker are silently discarded (`ON CONFLICT DO NOTHING`).

Indexes:
- `INDEX(broker)`
- `INDEX(delivered_at)`

### 4a. Atomic ingest pattern (race-condition safe)

With two ingest processes running concurrently, the following two-step pattern is safe:

**Step 1** — attempt to create the canonical alert row:
```sql
INSERT INTO alerts (lsst_diaObject_diaObjectId, ra_deg, dec_deg, ...)
VALUES (...)
ON CONFLICT (lsst_diaObject_diaObjectId) DO NOTHING
RETURNING id
```
- Row returned → **new alert**; enqueue a `crossmatch_alert` Celery task.
- Nothing returned → alert already exists (received from the other broker first); skip enqueue.

This is atomic under concurrent access. PostgreSQL guarantees exactly one INSERT wins,
so exactly one ingest process enqueues the crossmatch task — even if both ANTARES and
Lasair deliver the same alert within milliseconds of each other.

**Step 2** — record the broker delivery (always, regardless of Step 1 outcome):
```sql
INSERT INTO alert_deliveries (lsst_diaObject_diaObjectId, broker, broker_alert_id, raw_payload)
VALUES (...)
ON CONFLICT (lsst_diaObject_diaObjectId, broker) DO NOTHING
```
Re-deliveries from the same broker are silently discarded.

### 5. Separate ingest containers
Each broker runs as an independent long-lived process (management command), mirroring
the existing ANTARES pattern:
- `python manage.py run_antares_ingest`
- `python manage.py run_lasair_ingest`

Both write to the same `alerts` and `alert_deliveries` tables. Both are independently
restartable and scalable.

### 6. Lasair filter criteria (open — to be defined)
The design defines detailed ANTARES filter criteria (§2.1A). A companion section is
needed for Lasair covering: what topic we subscribe to, what Lasair-side filter (if any)
we apply, and what annotation fields Lasair provides that ANTARES does not.

## Changes Required in the Design Document

| Section | Change |
|---|---|
| Title | Drop "ANTARES + Gaia" → "ANTARES + Lasair + Gaia" (or "Multi-broker + Gaia") |
| §1 Goals | Add "multi-broker ingest for resilience and richer filtering" |
| §2.1 Components | Split component B into "ANTARES Ingest" and "Lasair Ingest"; add component B2 |
| New §2.1B2 | Lasair filter criteria (analogous to §2.1A ANTARES filter criteria) |
| §3 Data Flow | Add Lasair path parallel to ANTARES |
| §3.1 Sequence diagram | Add a Lasair lane |
| §4.1 | Add companion §4.5 "Lasair → Ingest" covering client lib, topic, auth |
| §5.2 | Add `alert_deliveries` table (§5.2.1b) |
| §8.2 Package layout | `antares/` → `brokers/antares/`, add `brokers/lasair/`, `brokers/normalize.py` |
| §8.3 Key processes | Add `run_lasair_ingest` management command |
| §9.1 Deployments | Add Lasair ingest Deployment |
| §9.1.3 Config | Add `LASAIR_TOPIC`, `LASAIR_*` credentials |
| §10 Open Questions | Add: Lasair Python client / Kafka config; Lasair topic name and auth; which Lasair annotations to store |

## Other Improvements Spotted

While reading the design, a few unrelated improvements are worth noting:

- **`alerts.schema_version` is ambiguous**: does this refer to the LSST alert packet
  schema version, the broker's schema version, or an internal format version? Should
  be clarified/renamed (e.g., `lsst_alert_schema_version`).
- **`notifications.destination` is a free-text field** (`e.g., lsst-http,
  kafka-topic`): consider an ENUM or a lookup table for consistency.
- **`crossmatch_runs` UNIQUE constraint is "optional"**: it should be made mandatory
  to prevent duplicate run rows under concurrent ingest.
- **`planned_pointings.source` has a DEFAULT 'heroic'**: once there's a second source
  this default should be removed and the column made required at the application layer.

## Lasair Client Details (Researched)

Lasair delivers alerts via **Apache Kafka** and provides a thin Python wrapper in the
`lasair` PyPI package (which wraps `confluent_kafka` internally).

### Connection

```
Kafka server: kafka.lsst.ac.uk:9092
Python package: lasair  (pip install lasair)
Dependency:     confluent_kafka
```

### Consuming alerts

```python
from lasair import lasair_consumer
consumer = lasair_consumer(
    kafka_server='kafka.lsst.ac.uk:9092',
    group_id='scimma-crossmatch-prod',   # stable string; tracks consumption position
    topic='lasair_<uid>_<filter-name>',
)
while True:
    msg = consumer.poll(timeout=20)
    if msg:
        alert = json.loads(msg.value())
```

### Topic naming

Topics are `"lasair_" + user_id + "_" + sanitised_filter_name`
(e.g., `lasair_42_high-snr-transients`). **Each Lasair filter produces its own topic.**
We need to create a filter on the Lasair web UI before we can subscribe.

### GroupID semantics

- **Keep GroupID constant in production** → Kafka delivers each message exactly once
  and resumes from where the consumer left off.
- **Change GroupID in testing** → re-reads all cached alerts (last ~7 days).

### Authentication

Authentication mechanism is not clearly documented for the Kafka stream. The REST API
uses a bearer token (`lasair.lasair_client(token=...)`), but `lasair_consumer` appears
to connect without explicit auth. **This needs to be confirmed before implementation.**

### Alert message format

Messages arrive as JSON. The top-level `objectId` field maps to `lsst_diaObject_diaObjectId`.
Lasair enriches the base LSST alert packet with its own classification fields. The exact
set of fields depends on the filter applied. **We should confirm which Lasair fields
we want to capture in `alert_deliveries.raw_payload`.**

### Requirements additions

- Add `lasair` and `confluent_kafka` to `requirements.base.txt`.
- Add environment variables: `LASAIR_KAFKA_SERVER`, `LASAIR_TOPIC`, `LASAIR_GROUP_ID`,
  and (if needed) `LASAIR_TOKEN`.

## Open Questions

1. **Lasair Kafka auth**: does `lasair_consumer` require credentials at all? If so,
   what format (SASL/PLAIN? API token passed as SASL password)?
2. **Lasair filter/topic**: what filter do we create on the Lasair UI — should it mirror
   the ANTARES filter criteria (SNR > 10, no dipole, no satellite artifacts, etc.)?
3. **Lasair alert schema**: what is the full set of fields in a Lasair alert JSON?
   Which fields map to `lsst_diaObject_diaObjectId` and the LSST positional fields?
4. **Lasair-specific annotations**: which Lasair-side fields (classification scores,
   cross-IDs, sherlock annotations, etc.) do we want in `alert_deliveries.raw_payload`?
5. **Concurrency safety on UPSERT + enqueue**: with two ingest processes writing
   concurrently, a SELECT-then-enqueue race is possible. Use `INSERT ... ON CONFLICT DO
   NOTHING RETURNING id` and only enqueue a crossmatch task if the INSERT created a new
   row (not an update).
