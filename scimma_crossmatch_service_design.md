# LSST Alert Matching Service Architecture (ANTARES + Lasair + Gaia + DES)

This document defines a Python-based service architecture that receives Rubin/LSST alerts from the **ANTARES** and **Lasair** brokers, matches them against the **Gaia DR3** and **DES Y6 Gold** catalogs using **LSDB**, and records results for eventual **feedback to LSST** (return mechanism TBD).

It is an iteration of the original design, updated to:
- Use **Celery** for work orchestration
- Use **Valkey** as the Celery broker/backing store (instead of NATS/JetStream)
- Use **LSDB native crossmatching** (`from_dataframe()` + `crossmatch()`) against HATS catalogs on S3
- Provide more concrete **PostgreSQL table design**, **Python modules**, and **Kubernetes + Docker Compose deployment details**

---

## 1. Goals and Non-Goals

### Goals
- Reliable ingestion of LSST alerts from **multiple brokers** (ANTARES + Lasair) for stream resilience and richer science filtering.
- Idempotent processing (safe to retry alert ingest, match jobs, and notifications).
- Horizontal scalability: multiple workers consuming queued crossmatch work.
- Separation of concerns: ingest vs. schedule ingest vs. match vs. notify.
- Observability: logs, metrics, tracing.
- K8s-native deployment using **existing container images** and **Helm charts**.
- Local development via **Docker Compose** using the *same container images*.

### Non-Goals
- Define the final “send-back-to-LSST” protocol (we only define internal interfaces and a placeholder service).
- Build a complete science-quality vetting pipeline.

---

## 2. High-Level Architecture

### 2.1 Components

**A. ANTARES Filter (runs in ANTARES infrastructure)**
- We supply a filter to ANTARES.
- Filter selects alerts of interest.
- Filter outputs alerts to our client subscription topic (populated via Locus tagging).

#### ANTARES Filter Selection Criteria (Initial)
The filter will restrict alerts to likely high-quality transient candidates using the following criteria:

- SNR greater than 10.
- Not saturated.
- Not near image edge.
- Not classified as dipole.
- Exclude known Solar System objects.

Specifically the LSST alert in the Locus must satisfy:

- `lsst_diaSource_snr > 10 AND`
- `lsst_diaSource_ssObjectId in (0, None) AND`
- `lsst_diaSource_psfFlux_flag == False AND`
- `lsst_diaSource_centroid_flag == False AND`
- `lsst_diaSource_shape_flag == False AND`
- `lsst_diaSource_isDipole == False AND`
- `lsst_diaSource_pixelFlags_saturated == False AND`
- `lsst_diaSource_pixelFlags_edge == False AND`
- `lsst_diaSource_pixelFlags_cr == False AND`
- `lsst_diaSource_pixelFlags_streak == False`

These criteria are intended to:
- Reduce broker-to-client traffic.
- Focus matching resources on astrophysically interesting candidates.
- Avoid artifacts and known moving objects.

The exact implementation will follow ANTARES filter syntax and available alert schema fields.

**B. Alert Ingest Services (runs in our Kubernetes cluster)**

Two independent ingest services consume from separate broker streams and write to the same shared `alerts` table.

**B1. ANTARES Ingest Service**
- Subscribes to the ANTARES topic produced by our filter.
- Validates/normalizes alert payload.
- UPSERTs the alert into `alerts` keyed by `lsst_diaObject_diaObjectId`.
- Records the delivery in `alert_deliveries` (broker=`'antares'`).
- Enqueues a `crossmatch_alert` Celery task **only if the UPSERT created a new row** (i.e., Lasair has not already delivered the same alert).

**B2. Lasair Ingest Service**
- Subscribes to a Lasair Kafka topic produced by our Lasair user-defined streaming filter.
- Validates/normalizes alert payload against the shared LSST field schema.
- UPSERTs the alert into `alerts` keyed by `lsst_diaObject_diaObjectId`.
- Records the delivery in `alert_deliveries` (broker=`'lasair'`).
- Enqueues a `crossmatch_alert` Celery task **only if the UPSERT created a new row** (i.e., ANTARES has not already delivered the same alert).

#### Lasair Filter Selection Criteria

The Lasair filter `reliability_moderate` has been created on the Lasair web UI and
produces the Kafka topic `lasair_366SCiMMA_reliability_moderate`.

**Filter SQL:**

```sql
SELECT
    objects.diaObjectId, objects.firstDiaSourceMjdTai, objects.ra, objects.decl
FROM objects
WHERE
    objects.nDiaSources >= 1
    AND objects.latestR > 0.6
    AND mjdnow() - objects.lastDiaSourceMjdTai < 1
```

**Field descriptions:**

| Field | Description |
|---|---|
| `diaObjectId` | LSST DIA object identifier — maps to `lsst_diaObject_diaObjectId` |
| `firstDiaSourceMjdTai` | MJD-TAI timestamp of the object's first detection — used as `event_time` |
| `ra`, `decl` | LSST positional fields (degrees) |

**Filter criteria semantics:**
- `nDiaSources >= 1` — any object with at least one detection. Minimal gate;
  the `latestR` threshold handles quality filtering.
- `latestR > 0.6` — LSST ML Real/Bogus score above 0.6. Lowered from 0.7 to
  admit more candidates while still rejecting the majority of artifacts. The
  filter name `reliability_moderate` reflects this threshold range.
- `lastDiaSourceMjdTai` within 1 day — only recent/active transients are
  delivered; avoids re-delivering old objects on Kafka replay.

**Comparison with ANTARES filter:**
The two filters are complementary rather than equivalent. ANTARES uses explicit
boolean flags plus an SNR threshold and Solar System exclusion; Lasair uses a
single ML score plus a recency window. Both paths write to the same `alerts`
table; the deduplication UPSERT ensures each `diaObjectId` is crossmatched
exactly once regardless of which broker delivers it first.

**C. Crossmatch Workers (Celery workers; runs in our Kubernetes cluster; horizontally scaled)**
- Consume batch crossmatch jobs from Celery.
- Use LSDB to match alerts against all configured HATS catalogs (currently **Gaia DR3** and **DES Y6 Gold**) via `lsdb.from_dataframe()` + `catalog.crossmatch()`.
- LSDB operates on HATS-formatted (HEALPix-partitioned Parquet) catalogs and uses **Dask** under the hood for parallel, distributed computation. When `DASK_SCHEDULER_ADDRESS` is set, each worker connects to a remote Dask scheduler at startup via `dask.distributed.Client`, offloading computation to dedicated Dask workers. When unset, Dask runs locally within the Celery worker process using the default synchronous scheduler.
- Catalogs are defined in a configurable registry (`CROSSMATCH_CATALOGS` in Django settings). Each entry specifies the catalog name, HATS URL, source ID column, and RA/Dec column names (which vary per catalog — e.g., lowercase `ra`/`dec` for Gaia, uppercase `RA`/`DEC` for DES).
- The Gaia DR3 HATS catalog is accessed from the **public S3 bucket** `s3://stpubdata/hats/gaia/dr3/` (no credentials required). The DES Y6 Gold HATS catalog is accessed from `https://data.lsdb.io/hats/des/des_y6_gold`.
- Each catalog object is cached in a module-level dict (`_catalog_cache`) keyed by catalog name within each worker process (metadata only; lightweight).
- Alert batches (up to 100k) are converted to an LSDB catalog via `from_dataframe()` with adaptive HEALPix partitioning, then crossmatched sequentially against each configured catalog. LSDB loads only the HATS partitions that overlap the alert positions.
- Per-catalog error isolation: if crossmatching fails for one catalog, the remaining catalogs are still processed.
- A later enhancement may introduce **locally cached copies** of relevant HATS partitions to reduce latency and egress costs.
- Record match outputs into PostgreSQL.

**E. Match Notifier Service (runs in our Kubernetes cluster)**
- Watches PostgreSQL for newly created matches.
- Sends an update/annotation back to LSST (mechanism TBD).
- Records notification attempts and outcomes for retries.

**F. Supporting Infrastructure**
- **PostgreSQL**: system of record (alerts, schedules, match results, notifications, job audit).
- **Valkey**: Celery broker/result backend.
- (Optional) Object storage/cache for LSDB/HATS data, depending on catalog deployment.

---

## 3. Data Flow

1. LSST publishes alert packets → ANTARES receives.
2. Our ANTARES filter selects a subset → ANTARES publishes to our subscription topic.
3. **Either** ingest service (ANTARES or Lasair) receives the alert:
   - UPSERT into `alerts` keyed by `lsst_diaObject_diaObjectId` (`ON CONFLICT DO NOTHING`)
   - INSERT into `alert_deliveries` recording the broker name and broker-specific envelope (`ON CONFLICT DO NOTHING`)
   - If the UPSERT created a new `alerts` row → submit Celery task `crossmatch_alert(lsst_diaObject_diaObjectId)`
   - If the UPSERT hit a conflict (alert already delivered by the other broker) → skip task enqueue
4. Crossmatch worker(s) run:
   - Celery Beat dispatches batch every 30s when thresholds are met
   - Load batch of QUEUED alerts into pandas DataFrame
   - Convert to LSDB catalog via `from_dataframe()`
   - Crossmatch sequentially against all configured HATS catalogs (Gaia DR3, DES Y6 Gold)
   - Write results to `catalog_matches` (one row per catalog match)
   - Transition all alerts in batch to MATCHED
6. Notifier service:
   - Detect new match rows
   - Send an LSST update (TBD)
   - Track in `notifications`

### 3.1 Sequence Diagram

```mermaid
sequenceDiagram
  autonumber
  participant LSST as LSST Alert Stream
  participant ANT as ANTARES Broker
  participant AFIL as ANTARES Filter
  participant AING as ANTARES Ingest
  participant LAS as Lasair Broker
  participant LING as Lasair Ingest
  participant PG as PostgreSQL
  participant RED as Valkey (Celery broker)
  participant CEL as Celery
  participant WRK as Crossmatch Workers
  participant LSDB as LSDB (Gaia, DES)
  participant NOT as Match Notifier
  participant LSSTRET as LSST Update Receiver (TBD)

  LSST->>ANT: Publish alert packet
  LSST->>LAS: Publish alert packet

  par ANTARES delivery
    ANT->>AFIL: Evaluate alert against ANTARES filter criteria
    alt Passes ANTARES filter
      ANT-->>AING: Stream alert on ANTARES topic
      AING->>PG: UPSERT alerts (ON CONFLICT DO NOTHING) + INSERT alert_deliveries (broker=antares)
      alt New alert (first delivery)
        AING->>CEL: Enqueue crossmatch_alert task
        CEL->>RED: Store task message
      end
    else Filtered out
      AFIL-->>ANT: Drop / no tag
    end
  and Lasair delivery
    LAS->>LING: Stream alert on Lasair Kafka topic (filtered by Lasair filter)
    LING->>PG: UPSERT alerts (ON CONFLICT DO NOTHING) + INSERT alert_deliveries (broker=lasair)
    alt New alert (first delivery)
      LING->>CEL: Enqueue crossmatch_alert task
      CEL->>RED: Store task message
    end
  end

  RED-->>WRK: Deliver batch task
  WRK->>PG: Load QUEUED alerts (batch_ids)
  WRK->>LSDB: from_dataframe() + crossmatch(gaia, des, ...)
  WRK->>PG: UPSERT catalog_matches + transition to MATCHED
  NOT->>PG: Watch for new matches
  NOT->>LSSTRET: Send match update (TBD)
  NOT->>PG: Record notifications state
```

---

## 4. Interfaces

### 4.1 ANTARES → Ingest

ANTARES delivers alerts via **Apache Kafka** using the `antares-client` PyPI package
(which wraps `confluent_kafka`). The `StreamingClient` abstracts Kafka consumer setup
including SASL authentication.

**Connection**:
- Python package: `antares-client[subscriptions]` (the `subscriptions` extra installs `confluent_kafka`)
- Source: https://gitlab.com/nsf-noirlab/csdc/antares/client

**Consuming alerts**:

```python
# brokers/antares/consumer.py
from antares_client import StreamingClient

client = StreamingClient(
    topics=[settings.ANTARES_TOPIC],       # e.g. ‘lsst_scimma_quality_transient’
    api_key=settings.ANTARES_API_KEY,      # credentials from ANTARES team
    api_secret=settings.ANTARES_API_SECRET,
    group=settings.ANTARES_GROUP_ID,       # stable string in production
)

for topic, locus in client.iter():
    newest_alert = locus.alerts[0]
    raw = newest_alert.properties          # flat dict with lsst_diaObject_*, ant_* keys
    canonical = normalize_antares(raw)
    ingest_alert(canonical, broker=’antares’)
```

**Data model**: `StreamingClient.iter()` yields `(topic, locus)` tuples. The `Locus`
object has top-level attributes (`locus_id`, `ra`, `dec`, `properties`) but the LSST
alert fields (`lsst_diaObject_*`, `lsst_diaSource_*`, `ant_*`) are in
`locus.alerts[0].properties`, not `locus.properties`. The `locus.properties` dict
contains only summary metadata (`num_alerts`, `brightest_alert_magnitude`, etc.).

**Filtering**: Not all alerts from ANTARES carry `lsst_diaObject_diaObjectId`. Alerts
missing this field are skipped with an info-level log (not treated as errors).

**GroupID semantics**:
- Keep the GroupID **constant in production** — Kafka uses it to track the consumer’s
  read position and delivers each message exactly once, resuming after restarts.
- Leave the GroupID **empty in development** — `settings.py` generates a unique
  timestamp-suffixed ID so each container restart replays all cached alerts.

**Authentication**: `StreamingClient` authenticates via SASL using `api_key` and
`api_secret` credentials obtained from the ANTARES team. Typically one set of
credentials per institution; only one active streaming client per credential set
unless authorized otherwise.

**Error handling**: Exponential backoff (1 s initial, 60 s max) on streaming errors.
On exception, the consumer reconnects by creating a new `StreamingClient`. Per-alert
ingestion errors are logged but do not interrupt the stream.

**Ingest requirements**:
- Reconnect/resume semantics via the Kafka GroupID (automatic on restart with a stable GroupID).
- Backpressure (limit concurrent DB writes; retry on DB unavailability).
- Deduplication keyed by `lsst_diaObject_diaObjectId` (UPSERT handles this; `alert_deliveries` UNIQUE constraint prevents duplicate delivery rows).

**Environment variables**:

| Variable | Example | Notes |
|---|---|---|
| `ANTARES_API_KEY` | `<api-key>` | SASL credential from ANTARES team |
| `ANTARES_API_SECRET` | `<api-secret>` | SASL credential from ANTARES team |
| `ANTARES_TOPIC` | `lsst_scimma_quality_transient` | topic name from ANTARES |
| `ANTARES_GROUP_ID` | `scimma-crossmatch-prod` | stable in production; empty in dev |

### 4.2 Ingest → Celery
We enqueue a Celery task with the minimal durable key (`lsst_diaObject_diaObjectId`). The worker loads all needed fields from Postgres to avoid large messages.

Recommended Celery task signature:
- `crossmatch_alert(lsst_diaObject_diaObjectId: str, match_version: int = 1)`

### 4.3 Lasair → Ingest

Lasair delivers alerts via **Apache Kafka** using the `lasair` PyPI package (which wraps `confluent_kafka`).

**Connection**:
- Kafka server: `lasair-lsst-kafka.lsst.ac.uk:9092`
- Python package: `lasair` (installs `confluent_kafka` as a dependency)

**Consuming alerts**:

```python
# brokers/lasair/ingest.py
import json
from lasair import lasair_consumer

consumer = lasair_consumer(
    kafka_server=settings.LASAIR_KAFKA_SERVER,   # lasair-lsst-kafka.lsst.ac.uk:9092
    group_id=settings.LASAIR_GROUP_ID,           # stable string in production
    topic=settings.LASAIR_TOPIC,                 # lasair_{uid}_{filter_name}
)
while True:
    msg = consumer.poll(timeout=20)
    if msg:
        alert = json.loads(msg.value())
        handle_alert(alert)
```

**Topic naming**: Topics follow the pattern `lasair_{user_id}_{sanitised_filter_name}`
(e.g., `lasair_42_high-snr-transients`). Topics are created via the Lasair web UI when
a streaming filter is saved. The topic name changes if the filter is renamed.

**GroupID semantics**:
- Keep the GroupID **constant in production** — Kafka uses it to track the consumer's
  read position and delivers each message exactly once, resuming after restarts.
- Change the GroupID in development/testing to replay all cached alerts (last ~7 days
  retained by the Kafka server).

**Authentication**: `lasair_consumer` connects to `lasair-lsst-kafka.lsst.ac.uk:9092`
**without any credentials** — no SASL username/password and no bearer token are
required. The Lasair REST API uses a bearer token (`lasair_client(token=...)`),
but this is not needed for the Kafka consumer ingest path.

**Ingest requirements**:
- Reconnect/resume semantics via the Kafka GroupID (automatic on restart with a stable GroupID).
- Backpressure (limit concurrent DB writes; retry on DB unavailability).
- Deduplication keyed by `lsst_diaObject_diaObjectId` (UPSERT handles this; alert_deliveries UNIQUE constraint prevents duplicate delivery rows).

**Environment variables**:

| Variable | Example | Notes |
|---|---|---|
| `LASAIR_KAFKA_SERVER` | `lasair-lsst-kafka.lsst.ac.uk:9092` | |
| `LASAIR_TOPIC` | `lasair_366SCiMMA_reliability_moderate` | created on Lasair web UI |
| `LASAIR_GROUP_ID` | `scimma-crossmatch-prod` | stable in production |
| `LASAIR_TOKEN` | `<api-token>` | REST API token (if needed for auth) |

### 4.4 Notifier → LSST (TBD)
We define a stable internal interface so multiple outbound mechanisms can be swapped in later.

```python
class LsstReturnClient(Protocol):
    def send_match_update(self, lsst_diaObject_diaObjectId: str, payload: dict) -> "DeliveryResult":
        ...
```

### 4.5 Notifier → SCiMMA Hopskotch

Crossmatch results are published to the **SCiMMA Hopskotch** Kafka service using
the `hop-client` PyPI package. This is the first concrete output channel; the LSST
return channel (§4.4) remains TBD.

**Publishing library**: `hop-client` on PyPI (wraps `confluent_kafka`).
- Source: https://github.com/scimma/hop-client
- Docs: https://hop-client.readthedocs.io/en/stable/

**Publishing messages**:

```python
# notifier/impl_hopskotch.py
from hop import Stream
from hop.auth import Auth

auth = Auth(user=settings.HOPSKOTCH_USERNAME, password=settings.HOPSKOTCH_PASSWORD)
stream = Stream(auth=auth)

url = f"{settings.HOPSKOTCH_BROKER_URL}/{settings.HOPSKOTCH_TOPIC}"
with stream.open(url, "w") as producer:
    producer.write(payload)   # plain dict → auto-serialized as JSON
```

**Message payload**: Each published message is a flat JSON dict containing the catalog name and source ID (generic across all catalogs):

```json
{
    "diaObjectId": 123456789,
    "ra": 150.123,
    "dec": 2.456,
    "catalog_name": "gaia_dr3",
    "catalog_source_id": "4567890123456789",
    "separation_arcsec": 0.42
}
```

**Notification lifecycle**:
1. `crossmatch_batch` creates `Notification` rows (state=`pending`,
   destination=`hopskotch`) alongside `CatalogMatch` rows.
2. A periodic Celery Beat task (`dispatch_notifications`, every 10 s) polls for
   `pending` notifications using `select_for_update(skip_locked=True)`.
3. The dispatcher groups notifications by `destination` and routes to the
   appropriate backend handler via a registry (`notifier/dispatch.py`).
4. The Hopskotch handler opens one Kafka connection per batch, publishes each
   notification individually, and marks each `sent` or `failed`.
5. After a batch, alerts with all notifications `sent` transition to `NOTIFIED`.

**Destination routing**: The `Notification.destination` field enables multiple
output channels. Hopskotch is the first backend (`destination='hopskotch'`).
Adding the LSST return channel requires only a new handler implementation
registered in `notifier/dispatch.py`.

**Authentication**: `hop.auth.Auth(user, password)` using SASL credentials
configured via environment variables.

**Error handling**: Per-notification failures are isolated — a failed publish
marks that notification `FAILED` with `last_error` but does not interrupt the
batch. No automatic retry in the initial implementation.

**Environment variables**:

| Variable | Example | Notes |
|---|---|---|
| `HOPSKOTCH_BROKER_URL` | `kafka://kafka.scimma.org` | Kafka broker URL |
| `HOPSKOTCH_TOPIC` | `crossmatch-results` | topic name for publishing |
| `HOPSKOTCH_USERNAME` | `<username>` | SASL credential |
| `HOPSKOTCH_PASSWORD` | `<password>` | SASL credential |

**Local Kafka for testing**: A local Kafka server (`scimma/server:latest` with
`--noSecurity`) can be used instead of production Hopskotch. In Docker Compose,
enable the `local-kafka` profile: `docker compose --profile local-kafka up`.
Then update `.env` to set `HOPSKOTCH_BROKER_URL=kafka://local-kafka:9092` and
`HOPSKOTCH_TOPIC=crossmatch-test`. Leave `HOPSKOTCH_USERNAME` empty — the
publisher automatically disables authentication when credentials are not set.

---

## 5. PostgreSQL Database Design

### 5.1 Conventions
- `TIMESTAMPTZ` for datetimes.
- `JSONB` for raw payload storage.
- Idempotent writes via **unique constraints + UPSERT**.
- Prefer natural unique keys (e.g., `lsst_diaObject_diaObjectId`) plus surrogate `BIGSERIAL` when helpful.

### 5.2 Tables

#### 5.2.1 `alerts`
Stores raw alerts and normalized fields.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | internal |
| lsst_diaobject_diaobjectid | BIGINT UNIQUE NOT NULL | stable identifier from alert |
| lsst_diasource_diasourceid | BIGINT NULL | candidate identifier |
| ra_deg | DOUBLE PRECISION NOT NULL | normalized |
| dec_deg | DOUBLE PRECISION NOT NULL | normalized |
| event_time | TIMESTAMPTZ NOT NULL | candidate/observation time |
| ingest_time | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| schema_version | INTEGER NOT NULL | alert schema version |
| payload | JSONB NOT NULL | raw payload |
| status | TEXT NOT NULL DEFAULT 'ingested' | ingested, queued, matched, notified |

Indexes:
- `UNIQUE(lsst_diaobject_diaobjectid)`
- `INDEX(event_time)`
- `INDEX(status)`
- Optional: `GIN(payload)` if querying payload fields.

#### 5.2.1b `alert_deliveries`
Records each broker delivery separately. Allows tracking which broker(s) delivered a
given alert, with per-broker metadata.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaobject_diaobjectid | BIGINT NOT NULL REFERENCES alerts(lsst_diaobject_diaobjectid) | |
| broker | TEXT NOT NULL | `'antares'` or `'lasair'` |
| broker_alert_id | TEXT NULL | broker-specific alert/event id if available |
| delivered_at | TIMESTAMPTZ NOT NULL DEFAULT now() | time of this delivery |
| raw_payload | JSONB NULL | broker-specific envelope/annotations (not the LSST payload, which lives in `alerts.payload`) |

Constraints:
- `UNIQUE(lsst_diaobject_diaobjectid, broker)` — one record per broker per alert; re-deliveries from the same broker are discarded with `ON CONFLICT DO NOTHING`.

Indexes:
- `INDEX(broker)`
- `INDEX(delivered_at)`

#### 5.2.2 `catalog_matches`
Stores match outputs for all catalog crossmatches (Gaia, DES, SkyMapper, etc.).

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaobject_diaobjectid | BIGINT NOT NULL REFERENCES alerts(lsst_diaobject_diaobjectid) | |
| catalog_name | TEXT NOT NULL | e.g., `'gaia_dr3'`, `'des_dr2'`, `'ps1_dr2'` |
| catalog_source_id | TEXT NOT NULL | Source identifier in the named catalog |
| match_distance_arcsec | DOUBLE PRECISION NOT NULL | angular separation |
| match_score | DOUBLE PRECISION NULL | optional scoring |
| source_ra_deg | DOUBLE PRECISION NULL | cached source position (optional) |
| source_dec_deg | DOUBLE PRECISION NULL | |
| catalog_payload | JSONB NULL | catalog-specific columns (e.g., parallax, mag) |
| match_version | INTEGER NOT NULL DEFAULT 1 | algorithm versioning |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Constraints:
- `UNIQUE(lsst_diaobject_diaobjectid, catalog_name, catalog_source_id, match_version)`

Indexes:
- `INDEX(lsst_diaobject_diaobjectid)`
- `INDEX(catalog_name)`
- `INDEX(catalog_source_id)`

> **Note on `catalog_source_id` type:** Gaia uses 64-bit integer IDs, but DES, PS1, and SkyMapper also use numeric IDs in different formats. Storing as TEXT is universally compatible without data loss.

#### 5.2.3 `crossmatch_runs`
Optional: tracks worker execution attempts for auditing and retries (recommended when using Celery).

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaobject_diaobjectid | BIGINT NOT NULL REFERENCES alerts(lsst_diaobject_diaobjectid) | |
| match_version | INTEGER NOT NULL DEFAULT 1 | |
| celery_task_id | TEXT NULL | for correlation |
| state | TEXT NOT NULL DEFAULT 'queued' | queued, running, succeeded, failed |
| attempts | INTEGER NOT NULL DEFAULT 0 | |
| started_at | TIMESTAMPTZ NULL | |
| finished_at | TIMESTAMPTZ NULL | |
| last_error | TEXT NULL | |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| updated_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Constraints:
- Optional: `UNIQUE(lsst_diaobject_diaobjectid, match_version)` if we only want one canonical run per version.

#### 5.2.4 `notifications`
Tracks outbound updates to LSST.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaobject_diaobjectid | BIGINT NOT NULL REFERENCES alerts(lsst_diaobject_diaobjectid) | |
| catalog_match_id | BIGINT NULL REFERENCES catalog_matches(id) | nullable if aggregated |
| destination | TEXT NOT NULL | e.g., lsst-http, kafka-topic |
| payload | JSONB NOT NULL | what we attempted to send |
| state | TEXT NOT NULL DEFAULT 'pending' | pending, sent, failed |
| attempts | INTEGER NOT NULL DEFAULT 0 | |
| last_error | TEXT NULL | |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| updated_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| sent_at | TIMESTAMPTZ NULL | |

Indexes:
- `INDEX(state)`
- `INDEX(lsst_diaobject_diaobjectid)`

### 5.3 Transaction Boundaries & Idempotency

**Ingest service (atomic two-step pattern)**

With ANTARES and Lasair ingest processes running concurrently, the following two-step
pattern is safe and race-condition-free under concurrent access:

```sql
-- Step 1: attempt to create the canonical alert row
INSERT INTO alerts (lsst_diaobject_diaobjectid, ra_deg, dec_deg, ...)
VALUES (...)
ON CONFLICT (lsst_diaobject_diaobjectid) DO NOTHING
RETURNING id;
-- Row returned → new alert → enqueue crossmatch_alert Celery task
-- Nothing returned → alert already ingested by the other broker → skip enqueue
```

PostgreSQL guarantees exactly one `INSERT` wins under concurrent access, so exactly one
ingest process enqueues the crossmatch task — even if both brokers deliver the same alert
within milliseconds of each other.

```sql
-- Step 2: record the broker delivery (always; idempotent)
INSERT INTO alert_deliveries (lsst_diaobject_diaobjectid, broker, broker_alert_id, raw_payload)
VALUES (...)
ON CONFLICT (lsst_diaobject_diaobjectid, broker) DO NOTHING;
-- Re-deliveries from the same broker are silently discarded
```

Both steps should be executed in a single database transaction.

Additionally:
- If Celery enqueue fails, keep DB state at `ingested` and retry enqueue.

**Crossmatch worker (batch)**
- Load QUEUED alerts by `batch_ids` into pandas DataFrame.
- Crossmatch via LSDB `from_dataframe()` + `crossmatch()`.
- Write `CatalogMatch` rows via `bulk_create(ignore_conflicts=True)`.
- Transition ALL alerts in batch to MATCHED (both matched and unmatched).
- On failure: revert all alerts in batch to INGESTED for retry in next batch.

**Notifier**
- Insert a `notifications` row before sending.
- Update to sent/failed; retry with backoff.

---

## 6. Queue / Task Orchestration (Celery + Valkey)

### 6.1 Why Celery
- Native Python task queue with mature retry/backoff primitives.
- Fits the “many workers pulling crossmatch jobs” pattern.
- Easy to run as separate Deployments in Kubernetes.

### 6.2 Valkey usage pattern
- Valkey is used as:
  - Celery broker (`redis://valkey:6379/0`)
  - (Optional) Celery result backend (`redis://valkey:6379/1`) **or** disable results if not needed.

### 6.3 Delivery semantics
- Celery provides *at-least-once execution*; tasks can be re-delivered if workers crash.
- We rely on DB idempotency (UPSERT + unique constraints) to make retries safe.

### 6.4 Celery configuration recommendations
- `task_acks_late=True` (ack after work completes)
- `worker_prefetch_multiplier=1` (avoid long task hoarding)
- `task_reject_on_worker_lost=True`
- Per-task retry policy (e.g., `autoretry_for=(Exception,)`, `retry_backoff=True`, `max_retries=N`)

---

## 7. LSDB Multi-Catalog Crossmatch Design

### 7.1 LSDB Native Batch Crossmatching

LSDB is designed to efficiently perform large-catalog crossmatches by leveraging **HATS-formatted (HEALPix Adaptive Tiling Scheme) catalogs, lazy evaluation, and Dask parallelism**. Instead of loading entire catalogs into memory, LSDB only reads the HATS partitions that spatially overlap the input data.

### Batch Crossmatch Workflow

Alerts are processed in batches (up to 100k per batch). The crossmatch workflow uses LSDB’s `from_dataframe()` + `crossmatch()` API:

1. **Load QUEUED alerts** into a pandas DataFrame with `uuid`, `lsst_diaObject_diaObjectId`, `ra_deg`, `dec_deg`.
2. **Convert to LSDB catalog** via `lsdb.from_dataframe(df, ra_column=’ra_deg’, dec_column=’dec_deg’)`. This assigns adaptive HEALPix partitioning (orders 0-7) based on the alert sky positions. The LSDB alerts catalog is built once and reused for all reference catalogs.
3. **Loop through configured catalogs** (`settings.CROSSMATCH_CATALOGS`). For each catalog:
   - Load/cache the HATS catalog via `lsdb.open_catalog()` with catalog-specific columns (source ID, RA, Dec).
   - Crossmatch via `alerts_catalog.crossmatch(catalog, n_neighbors=1, radius_arcsec=CROSSMATCH_RADIUS_ARCSEC, suffixes=(‘_alert’, ‘_catalog’), suffix_method=’overlapping_columns’)`. LSDB only loads HATS partitions overlapping the alert positions.
   - Compute results via `matches.compute()`, returning a pandas DataFrame with a `_dist_arcsec` column.
   - Write `CatalogMatch` and `Notification` rows for each match.
   - Per-catalog error isolation: if one catalog fails, remaining catalogs are still processed.
4. **Transition all alerts** in the batch to MATCHED after all catalogs are processed.

```python
import lsdb
from django.conf import settings

# Module-level cache: {catalog_name: lsdb_catalog}
_catalog_cache = {}

def _get_catalog(catalog_config):
    name = catalog_config[‘name’]
    if name not in _catalog_cache:
        _catalog_cache[name] = lsdb.open_catalog(
            catalog_config[‘hats_url’],
            columns=[catalog_config[‘source_id_column’],
                     catalog_config[‘ra_column’],
                     catalog_config[‘dec_column’]],
        )
    return _catalog_cache[name]

def crossmatch_alerts(alerts_catalog, catalog_config):
    catalog = _get_catalog(catalog_config)
    matches = alerts_catalog.crossmatch(
        catalog, n_neighbors=1,
        radius_arcsec=settings.CROSSMATCH_RADIUS_ARCSEC,
        suffixes=(‘_alert’, ‘_catalog’),
        suffix_method=’overlapping_columns’,
    )
    return matches.compute()
```

### Why Not Visit-Based Spatial Constraints

The original design considered using Rubin telescope pointing data (via HEROIC) to constrain spatial queries. This was removed because:

- **Alerts have precise coordinates** — visit pointings only give ~3.5 degree field centers, far less precise than the alert RA/Dec.
- **HATS uses adaptive partitioning** — LSDB’s `from_dataframe()` handles partition alignment automatically. No manual HEALPix cell grouping needed.
- **Visit footprints are too large** — a single Rubin visit (~9.6 sq deg) spans dozens of HATS partitions, loading many tiles with no alerts.
- **Alerts span multiple visits** — a batch of 100k alerts may come from multiple visits, nights, and filters.
- **No external dependency** — eliminates the HEROIC API dependency, periodic sync task, and associated failure modes.

See `healpix_vs_visit_crossmatch.md` for the full analysis.

### Margin Caches and Edge Effects

LSDB supports **margin caches** — additional overlap regions around HEALPix partitions so that objects near tile boundaries are not missed. At a 1 arcsec crossmatch radius, edge effects are negligible. If the Gaia DR3 HATS catalog ships with a margin cache, `open_catalog()` picks it up automatically.

### 7.2 Match policy (initial)
- Store the best match (nearest neighbor) within a configurable radius.
- Config: `CROSSMATCH_RADIUS_ARCSEC` (default 1.0 arcsec; configurable via env var).
- `n_neighbors=1` (hardcoded; add configurable setting only when a science use case emerges).
- Tie-breaking: smallest separation (handled by LSDB's KDTreeCrossmatch).

### 7.3 Catalog Registry and Expansion

The system uses a configurable catalog registry (`CROSSMATCH_CATALOGS` in Django settings) that currently includes:

- **Gaia DR3** — accessed from `s3://stpubdata/gaia/gaia_dr3/public/hats` (source ID: `source_id`, RA/Dec columns: `ra`/`dec`)
- **DES Y6 Gold** — accessed from `https://data.lsdb.io/hats/des/des_y6_gold` (source ID: `COADD_OBJECT_ID`, RA/Dec columns: `RA`/`DEC`)

Each catalog entry specifies: `name`, `hats_url`, `source_id_column`, `ra_column`, `dec_column`. RA/Dec column names vary per catalog (e.g., lowercase for Gaia, uppercase for DES).

Planned future catalogs:

- **DECam Local Volume Exploration Survey (DELVE)**
- **SkyMapper**
- **Pan-STARRS1 (PS1)**

Adding a new catalog requires only a new entry in `CROSSMATCH_CATALOGS` and the corresponding `{CATALOG}_HATS_URL` env var. No changes to the core ingestion, queueing, matching logic, or deployment architecture are needed.

### 7.4 Dask Cluster Requirements

When using a remote Dask scheduler (via `DASK_SCHEDULER_ADDRESS`), both the **scheduler and workers** must have the same Python packages installed as the crossmatch-service. Dask uses pickle serialization to transfer task graphs between the client (Celery worker), scheduler, and Dask workers. If any component is missing a required module, deserialization fails with `ModuleNotFoundError`.

**Required packages on Dask scheduler and workers:**
- `lsdb` (and its transitive dependencies: `nested_pandas`, `hats`, `mocpy`, etc.)
- `numpy`, `pandas` — pinned to the same versions as the crossmatch-service
- `s3fs` — for reading HATS catalogs from S3
- Python version must match (currently 3.12)

**Local development:** Docker Compose includes optional `dask-scheduler` and `dask-worker` services behind a `dask-scheduler` profile, using the official Dask image (`ghcr.io/dask/dask`) with packages installed at startup via the `EXTRA_PIP_PACKAGES` environment variable. Activate with `docker compose --profile dask-scheduler up`. When the profile is not active, Dask runs locally within each Celery worker using its default synchronous scheduler.

**Kubernetes production:** The Dask cluster is managed as a separate project, not by the crossmatch-service Helm chart. The Dask cluster is shared infrastructure used by multiple consumers. The crossmatch-service connects to it via the `DASK_SCHEDULER_ADDRESS` env var (set in Helm values to the Kubernetes service DNS name). Responsibility for ensuring the Dask cluster has compatible packages installed lies with the Dask cluster project.

---

## 8. Python Implementation

### 8.1 Runtime and libraries
- Python 3.12 (constrained by Dask cluster compatibility)

Core libraries:
- **Web/ORM framework:** **Django** (Django ORM + built-in migrations)
- **Queue:** `celery`, `valkey`
- **DB driver:** `psycopg` (v3) via Django’s PostgreSQL backend
- **Config:** `pydantic-settings` (optional) or Django settings module with environment variable parsing (e.g., `django-environ`)
- **HTTP:** `httpx` (for future LSST return)
- **Time conversions:** `astropy` (for MJD ↔ datetime as needed)
- **Observability:** `structlog`, `prometheus-client`, optional `opentelemetry-sdk`
- **LSDB:** `lsdb` Python APIs

Rationale:
- We use **Django** for the ORM and migrations to align with a potential future web UI that exposes system state.
- This also aligns with the SCiMMA **Blast** application’s established stack (Django + Celery + Valkey + PostgreSQL), reducing operational and developer friction.

### 8.2 Suggested package layout

```
crossmatch/
  manage.py
  requirements.base.txt
  entrypoints/
    django_init.sh
    run_antares_ingest.sh
    run_celery_beat.sh
    run_celery_worker.sh
    run_flower.sh
    wait-for-it.sh
  project/
    __init__.py
    celery.py            # Celery app configured from Django settings
    settings.py
    management/
      __init__.py
      commands/
        __init__.py
        initialize_periodic_tasks.py
        run_antares_ingest.py
        run_lasair_ingest.py   # Lasair Kafka consumer loop (planned)
        run_notifier.py
  core/
    __init__.py
    apps.py              # Django AppConfig (to be created)
    models.py            # Django models for alerts/matches/pointings/notifications
    migrations/
  brokers/               # TARGET LAYOUT — current code has antares/ at top level pending this refactor
    __init__.py
    normalize.py         # shared LSST field extraction (ra, dec, diaObjectId, ...)
    antares/
      __init__.py
      ingest.py          # ANTARES StreamingClient runner (invoked via management command)
      normalize.py       # ANTARES-specific annotation handling
    lasair/
      __init__.py
      ingest.py          # lasair_consumer runner (invoked via management command)
      normalize.py       # Lasair-specific annotation handling
  matching/
    __init__.py
    catalog.py
  notifier/
    __init__.py
    watch.py
    lsst_return.py
    impl_http.py
  tasks/
    __init__.py
    crossmatch.py
    schedule.py
```

### 8.3 Key processes (containers)
We will run the long-lived processes as Django management commands (so they share settings, logging, ORM initialization, and consistent configuration).

- **ANTARES ingest service**: `python manage.py run_antares_ingest`
- **Lasair ingest service**: `python manage.py run_lasair_ingest`
- **Celery worker(s)** (crossmatch): `celery -A project worker -Q crossmatch -l INFO`
- **Celery beat**: `celery -A project beat` (dispatches crossmatch batches every 30s)
- **Notifier**: `python manage.py run_notifier`

Database schema changes are managed with Django migrations:
- `python manage.py makemigrations`
- `python manage.py migrate`

### 8.4 Celery task definitions

- `tasks.crossmatch.crossmatch_batch(batch_ids: list, match_version: int = 1)` — processes a batch of alert UUIDs through LSDB crossmatch against all configured catalogs (Gaia DR3, DES Y6 Gold).
- `tasks.schedule.dispatch_crossmatch_batch()` — periodic task (every 30s) that checks batch thresholds and dispatches `crossmatch_batch` with the selected alert IDs.

---

## 9. Deployment

### 9.1 Kubernetes

Deployments (recommended):
- `ingest` Deployment (1–N replicas; ANTARES Kafka consumer)
- `lasair-ingest` Deployment (1 replica; Lasair Kafka consumer)
- `worker-crossmatch` Deployment (N replicas)
- `notifier` Deployment (1–2 replicas)
- `celery-beat` Deployment (1 replica; dispatches crossmatch batches)

Dependencies:
- PostgreSQL (external managed or in-cluster)
- Valkey (in-cluster)

#### 9.1.1 Container images
- Prefer **one service image** with multiple entrypoints/commands.
- All components run the same image tag (ensures reproducibility).

#### 9.1.2 Helm chart approach
We will create a top-level Helm chart `alertmatch` that deploys:
- Our services (ingest, workers, notifier, schedule)
- Optional dependency charts:
  - `valkey`
  - `postgresql` (dev/test only; prod may use managed Postgres)

Values will define:
- image repository/tag
- env vars
- secrets
- replica counts
- CPU/memory requests/limits
- node affinity/tolerations (if LSDB needs larger nodes)

#### 9.1.3 Configuration & secrets
Environment variables (examples):
- `DATABASE_URL=postgresql+psycopg://user:pass@postgres:5432/alertmatch`
- `CELERY_BROKER_URL=redis://valkey:6379/0`
- `CELERY_RESULT_BACKEND=redis://valkey:6379/1` (optional)
- `ANTARES_TOPIC=...`
- `GAIA_HATS_URL=s3://stpubdata/gaia/gaia_dr3/public/hats`
- `DES_HATS_URL=https://data.lsdb.io/hats/des/des_y6_gold`
- `CROSSMATCH_RADIUS_ARCSEC=1.0`
- `DASK_SCHEDULER_ADDRESS=tcp://<host>:<port>` (optional; when unset, Dask runs locally)
- `LASAIR_KAFKA_SERVER=lasair-lsst-kafka.lsst.ac.uk:9092`
- `LASAIR_TOPIC=lasair_<uid>_<filter-name>`
- `LASAIR_GROUP_ID=scimma-crossmatch-prod`

Secrets:
- DB password
- ANTARES credentials
- LSST return credentials (future)

#### 9.1.4 Health checks
- Ingest: readiness requires DB connectivity and successful ANTARES client init.
- Workers: readiness requires DB + LSDB catalog reachable.
- Notifier: readiness requires DB.

#### 9.1.5 Observability in cluster
- Expose Prometheus metrics via a small HTTP server per process (e.g., `prometheus_client.start_http_server`).
- Structured logs to stdout.

### 9.2 Local Development (Docker Compose)

Local development uses Docker Compose with the **same images** as in Kubernetes.

Services:
- `postgres`
- `valkey`
- `ingest`
- `worker`
- `notifier`
- `celery-beat` (dispatches crossmatch batches)

Notes:
- Use `.env` for configuration.
- If you want live code edits, either:
  - build a `:dev` image variant that mounts source, or
  - use `docker compose build` frequently.

---

## 10. Open Questions / Decisions Needed

1. **LSST return channel**: what mechanism do we implement first (HTTP endpoint? Kafka? Rubin-specific API)?
2. **ANTARES topic and auth**: exact configuration fields for `StreamingClient` (topic name, resume semantics).
3. ~~**Match radius and columns**~~ — **Resolved**: 1 arcsec default radius, configurable via `CROSSMATCH_RADIUS_ARCSEC`. Store `source_id`, `ra`, `dec`, and match distance. Consumers query Gaia directly for additional fields.
4. ~~**Planned footprint gating**~~ — **Resolved**: removed. LSDB native crossmatching uses alert RA/Dec directly; no pointing constraints needed.
5. ~~**HEROIC API details**~~ — **Resolved**: HEROIC integration removed. See `healpix_vs_visit_crossmatch.md` for rationale.
6. ~~**Lasair Kafka auth**~~ — **Resolved**: `lasair_consumer` connects to `lasair-lsst-kafka.lsst.ac.uk:9092` without credentials. No SASL config or token required for the ingest path.
7. ~~**Lasair filter/topic**~~ — **Resolved**: filter `reliability_moderate` created on Lasair web UI; topic `lasair_366SCiMMA_reliability_moderate`. Criteria: `latestR > 0.7` AND `nDiaSources >= 1` AND last detection within 1 day. See §2.1 B2 for full SQL.
8. **Lasair alert schema**: what is the full JSON schema of a Lasair alert? Lasair uses `objectId` as the top-level key — confirm this is always identical to `lsst_diaObject_diaObjectId`. Confirm which field maps to LSST positional fields (RA/Dec).
9. **Lasair annotations to store**: which Lasair-side fields (Sherlock cross-matches, classification scores, etc.) should be preserved in `alert_deliveries.raw_payload`?

---

## 11. Appendices

### 11.1 Suggested first implementation milestone
- Ingest alert → store in DB → batch dispatcher enqueues crossmatch task
- Crossmatch worker: batch match against Gaia DR3 and DES Y6 Gold via LSDB (`from_dataframe()` + `crossmatch()`)
- Store match rows in `catalog_matches` (one row per catalog match)
- Notifier: dummy implementation (logs payload) + `notifications` bookkeeping

