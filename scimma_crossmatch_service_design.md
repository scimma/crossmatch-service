# LSST Alert Matching Service Architecture (ANTARES + Lasair + Gaia)

This document defines a Python-based service architecture that receives Rubin/LSST alerts from the **ANTARES** and **Lasair** brokers, matches them against the **Gaia** catalog using **LSDB**, incorporates Rubin pointing forecasts via **HEROIC**, and records results for eventual **feedback to LSST** (return mechanism TBD).

It is an iteration of the original design, updated to:
- Use **Celery** for work orchestration
- Use **Redis** as the Celery broker/backing store (instead of NATS/JetStream)
- Add a **HEROIC schedule ingestion worker** and associated database tables
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
| `lastDiaSourceMjdTai` | MJD of the most recent detection |
| `latestR` | Lasair Real/Bogus ML score for the latest source (0–1; 1 = real transient) |
| `age` | Computed days since last detection (display only; not stored) |

**Filter criteria semantics:**
- `nDiaSources >= 1` — any object with at least one detection. Minimal gate;
  the `latestR` threshold handles quality filtering.
- `latestR > 0.7` — LSST ML Real/Bogus score above 0.7. This is taken from the
  LSST alert reliability value that acts as a single proxy for the many individual
  artifact flags used by the ANTARES filter (dipole, streak, saturation, edge,
  cosmic ray). The filter name `reliability_moderate` reflects this threshold.
- `lastDiaSourceMjdTai` within 1 day — only recent/active transients are
  delivered; avoids re-delivering old objects on Kafka replay.

**Comparison with ANTARES filter:**
The two filters are complementary rather than equivalent. ANTARES uses explicit
boolean flags plus an SNR threshold and Solar System exclusion; Lasair uses a
single ML score plus a recency window. Both paths write to the same `alerts`
table; the deduplication UPSERT ensures each `diaObjectId` is crossmatched
exactly once regardless of which broker delivers it first.

**C. HEROIC Schedule Ingest Worker (runs in our Kubernetes cluster)**
- Periodically fetches planned Rubin pointings from HEROIC (which itself ingests the ObsLocTAP schedule endpoint).
- Stores planned pointings (center + FOV + time window + derived radius) into PostgreSQL.
- The intent is that crossmatch workers can use these pointings to constrain LSDB queries by sky region/patch.

**D. Crossmatch Workers (Celery workers; runs in our Kubernetes cluster; horizontally scaled)**
- Consume match jobs from Celery.
- Use LSDB to match alerts against the Gaia catalog.
- LSDB operates on HATS-formatted (HEALPix-partitioned Parquet) catalogs and uses **Dask** under the hood for parallel, distributed computation. Each worker process will invoke LSDB APIs, which internally construct Dask task graphs for cone searches or crossmatches and execute them locally within the worker container.
- Initially, the Gaia DR3 HATS catalog will be accessed directly from an **S3 bucket in us-east** (e.g., `s3://.../gaia_dr3`), with no local persistent cache inside the Kubernetes cluster.
- This means:
  - Each worker must have network access to read from the public S3 bucket in us-east.
  - Performance will depend on S3 throughput and network latency to us-east.
  - No shared on-cluster object cache is assumed in the first implementation.
- A later enhancement may introduce a **locally cached copy** of the relevant Gaia HATS partitions (e.g., via a PersistentVolume, object cache, or pre-staged subset of the catalog) to reduce latency and egress costs.
- Consult planned pointings (from PostgreSQL) to restrict matching/search to relevant sky regions when feasible.
- Record match outputs into PostgreSQL.

**E. Match Notifier Service (runs in our Kubernetes cluster)**
- Watches PostgreSQL for newly created matches.
- Sends an update/annotation back to LSST (mechanism TBD).
- Records notification attempts and outcomes for retries.

**F. Supporting Infrastructure**
- **PostgreSQL**: system of record (alerts, schedules, match results, notifications, job audit).
- **Redis**: Celery broker/result backend.
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
4. HEROIC schedule ingestion worker periodically refreshes planned pointings:
   - Fetch from HEROIC API
   - Replace/refresh `planned_pointings` table
5. Crossmatch worker(s) run:
   - Load alert coordinates/time from DB
   - Optionally constrain search using planned pointings
   - Use LSDB to crossmatch with Gaia
   - Write results to `gaia_matches`
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
  participant RED as Redis (Celery broker)
  participant CEL as Celery
  participant SCH as Schedule Ingest Worker
  participant WRK as Crossmatch Workers
  participant LSDB as LSDB (Gaia)
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

  RED-->>WRK: Deliver task
  WRK->>PG: Read alert + planned pointings
  WRK->>LSDB: Gaia crossmatch (cone/patch constrained)
  WRK->>PG: UPSERT gaia_matches
  NOT->>PG: Watch for new matches
  NOT->>LSSTRET: Send match update (TBD)
  NOT->>PG: Record notifications state

  loop Every N minutes
    SCH->>PG: Refresh planned_pointings from HEROIC API
  end
```

---

## 4. Interfaces

### 4.1 ANTARES → Ingest
We use ANTARES’ Python client (e.g., `StreamingClient`) to subscribe to the topic populated by our filter.

Ingest must support:
- Reconnect/resume semantics supported by ANTARES streaming.
- Backpressure (limit concurrent writes; drop into retries if DB down).
- Deduplication keyed by `lsst_diaObject_diaObjectId`.

### 4.2 Ingest → Celery
We enqueue a Celery task with the minimal durable key (`lsst_diaObject_diaObjectId`). The worker loads all needed fields from Postgres to avoid large messages.

Recommended Celery task signature:
- `crossmatch_alert(lsst_diaObject_diaObjectId: str, match_version: int = 1)`

### 4.3 HEROIC → Schedule Ingest Worker
HEROIC provides planned pointings derived from the ObsLocTAP schedule feed. Each pointing includes `s_ra`, `s_dec`, `s_fov`, `t_min`, `t_max`, etc.

Key HEROIC notes:
- `s_region` is empty in the source schedule; FOV is provided as `s_fov` diameter.
- For LSSTCam, `s_fov = 3.0°` diameter → **radius = 1.5° (5400 arcsec)**.
- HEROIC refreshes planned pointings periodically (commonly every ~10 minutes) by deleting old planned pointings and inserting the refreshed set.

The schedule ingest worker should mirror this behavior in our Postgres table so workers always query a current set of “planned future pointings.”

### 4.5 Lasair → Ingest

Lasair delivers alerts via **Apache Kafka** using the `lasair` PyPI package (which wraps `confluent_kafka`).

**Connection**:
- Kafka server: `kafka.lsst.ac.uk:9092`
- Python package: `lasair` (installs `confluent_kafka` as a dependency)

**Consuming alerts**:

```python
# brokers/lasair/ingest.py
import json
from lasair import lasair_consumer

consumer = lasair_consumer(
    kafka_server=settings.LASAIR_KAFKA_SERVER,   # kafka.lsst.ac.uk:9092
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

**Authentication**: `lasair_consumer` connects to `kafka.lsst.ac.uk:9092`
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
| `LASAIR_KAFKA_SERVER` | `kafka.lsst.ac.uk:9092` | |
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
| lsst_diaObject_diaObjectId | TEXT UNIQUE NOT NULL | stable identifier from alert |
| lsst_diaSource_diaSourceId | TEXT NULL | candidate identifier |
| ra_deg | DOUBLE PRECISION NOT NULL | normalized |
| dec_deg | DOUBLE PRECISION NOT NULL | normalized |
| event_time | TIMESTAMPTZ NOT NULL | candidate/observation time |
| ingest_time | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| schema_version | INTEGER NOT NULL | alert schema version |
| payload | JSONB NOT NULL | raw payload |
| status | TEXT NOT NULL DEFAULT 'ingested' | ingested, queued, matched, notified |

Indexes:
- `UNIQUE(lsst_diaObject_diaObjectId)`
- `INDEX(event_time)`
- `INDEX(status)`
- Optional: `GIN(payload)` if querying payload fields.

#### 5.2.1b `alert_deliveries`
Records each broker delivery separately. Allows tracking which broker(s) delivered a
given alert, with per-broker metadata.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaObject_diaObjectId | TEXT NOT NULL REFERENCES alerts(lsst_diaObject_diaObjectId) | |
| broker | TEXT NOT NULL | `'antares'` or `'lasair'` |
| broker_alert_id | TEXT NULL | broker-specific alert/event id if available |
| delivered_at | TIMESTAMPTZ NOT NULL DEFAULT now() | time of this delivery |
| raw_payload | JSONB NULL | broker-specific envelope/annotations (not the LSST payload, which lives in `alerts.payload`) |

Constraints:
- `UNIQUE(lsst_diaObject_diaObjectId, broker)` — one record per broker per alert; re-deliveries from the same broker are discarded with `ON CONFLICT DO NOTHING`.

Indexes:
- `INDEX(broker)`
- `INDEX(delivered_at)`

#### 5.2.2 `planned_pointings`
Stores the *planned* Rubin pointings as exposed by the HEROIC API. This table is intentionally limited to the distilled fields that HEROIC persists (derived from the Rubin ObsLocTAP schedule) and then returns via its API.

Key HEROIC semantics:
- Pointings are **planned** (HEROIC sets `planned=True` based on `execution_status = "Not Observed"` in the upstream schedule).
- HEROIC constructs a **circular field/polygon** from `(s_ra, s_dec)` and the instrument FOV (for LSSTCam, `s_fov=3.0°` diameter → **1.5° radius**).
- HEROIC refresh behavior: on each ingest cycle (commonly ~10 minutes), existing planned pointings are deleted and replaced with the refreshed set.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | internal |
| source | TEXT NOT NULL DEFAULT 'heroic' | provenance |
| heroic_pointing_id | TEXT NULL | if HEROIC provides a stable id per pointing (preferred to store if available) |
| obs_id | TEXT NULL | upstream schedule `obs_id` when present |
| target_name | TEXT NULL | upstream schedule `target_name` when present |
| planned | BOOLEAN NOT NULL DEFAULT TRUE | HEROIC planned flag |
| s_ra_deg | DOUBLE PRECISION NOT NULL | pointing center RA (degrees) |
| s_dec_deg | DOUBLE PRECISION NOT NULL | pointing center Dec (degrees) |
| radius_deg | DOUBLE PRECISION NOT NULL | for LSSTCam typically 1.5 (derived from instrument FOV) |
| field_geojson | JSONB NULL | HEROIC-constructed circular polygon/field (store as GeoJSON-like structure returned by HEROIC) |
| t_min_mjd | DOUBLE PRECISION NOT NULL | observation window start (MJD) |
| t_max_mjd | DOUBLE PRECISION NOT NULL | observation window end (MJD) |
| t_planning_mjd | DOUBLE PRECISION NULL | planning timestamp (MJD) if provided |
| instrument_name | TEXT NULL | HEROIC instrument reference/name/slug if returned (e.g., LSSTCam or a canonical instrument id) |
| ingest_time | TIMESTAMPTZ NOT NULL DEFAULT now() | when we refreshed from HEROIC |

Constraints / indexes:
- Recommended uniqueness (best-effort; adjust once we confirm HEROIC’s id semantics):
  - If `heroic_pointing_id` exists: `UNIQUE(source, heroic_pointing_id)`
  - Else: `UNIQUE(source, obs_id, t_min_mjd, s_ra_deg, s_dec_deg)`
- `INDEX(ingest_time)`
- `INDEX(t_min_mjd, t_max_mjd)`
- Optional: `GIN(field_geojson)` if you expect to query by polygon content (not required initially).

Implementation note:
- Workers should primarily filter pointings by time window (`t_min_mjd/t_max_mjd`) and then compute angular distance to `(s_ra_deg, s_dec_deg)` in Python using `radius_deg` (no PostGIS required for v1).

#### 5.2.3 `gaia_matches`
Stores match outputs.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaObject_diaObjectId | TEXT NOT NULL REFERENCES alerts(lsst_diaObject_diaObjectId) | |
| gaia_source_id | BIGINT NOT NULL | Gaia source identifier |
| match_distance_arcsec | DOUBLE PRECISION NOT NULL | angular separation |
| match_score | DOUBLE PRECISION NULL | optional scoring |
| gaia_ra_deg | DOUBLE PRECISION NULL | cached Gaia position (optional) |
| gaia_dec_deg | DOUBLE PRECISION NULL | |
| gaia_payload | JSONB NULL | optional Gaia columns |
| match_version | INTEGER NOT NULL DEFAULT 1 | algorithm versioning |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Constraints:
- `UNIQUE(lsst_diaObject_diaObjectId, gaia_source_id, match_version)`

Indexes:
- `INDEX(lsst_diaObject_diaObjectId)`
- `INDEX(gaia_source_id)`

#### 5.2.4 `crossmatch_runs`
Optional: tracks worker execution attempts for auditing and retries (recommended when using Celery).

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaObject_diaObjectId | TEXT NOT NULL REFERENCES alerts(lsst_diaObject_diaObjectId) | |
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
- Optional: `UNIQUE(lsst_diaObject_diaObjectId, match_version)` if we only want one canonical run per version.

#### 5.2.5 `notifications`
Tracks outbound updates to LSST.

| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | |
| lsst_diaObject_diaObjectId | TEXT NOT NULL REFERENCES alerts(lsst_diaObject_diaObjectId) | |
| gaia_match_id | BIGINT NULL REFERENCES gaia_matches(id) | nullable if aggregated |
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
- `INDEX(lsst_diaObject_diaObjectId)`

### 5.3 Transaction Boundaries & Idempotency

**Ingest service (atomic two-step pattern)**

With ANTARES and Lasair ingest processes running concurrently, the following two-step
pattern is safe and race-condition-free under concurrent access:

```sql
-- Step 1: attempt to create the canonical alert row
INSERT INTO alerts (lsst_diaObject_diaObjectId, ra_deg, dec_deg, ...)
VALUES (...)
ON CONFLICT (lsst_diaObject_diaObjectId) DO NOTHING
RETURNING id;
-- Row returned → new alert → enqueue crossmatch_alert Celery task
-- Nothing returned → alert already ingested by the other broker → skip enqueue
```

PostgreSQL guarantees exactly one `INSERT` wins under concurrent access, so exactly one
ingest process enqueues the crossmatch task — even if both brokers deliver the same alert
within milliseconds of each other.

```sql
-- Step 2: record the broker delivery (always; idempotent)
INSERT INTO alert_deliveries (lsst_diaObject_diaObjectId, broker, broker_alert_id, raw_payload)
VALUES (...)
ON CONFLICT (lsst_diaObject_diaObjectId, broker) DO NOTHING;
-- Re-deliveries from the same broker are silently discarded
```

Both steps should be executed in a single database transaction.

Additionally:
- Create/UPSERT a `crossmatch_runs` row (queued) when enqueuing.
- If Celery enqueue fails, keep DB state at `ingested` and retry enqueue.

**Crossmatch worker**
- Mark `crossmatch_runs.state=running`, increment `attempts`.
- Compute and write matches via UPSERT.
- Mark run succeeded/failed.

**Notifier**
- Insert a `notifications` row before sending.
- Update to sent/failed; retry with backoff.

---

## 6. Queue / Task Orchestration (Celery + Redis)

### 6.1 Why Celery
- Native Python task queue with mature retry/backoff primitives.
- Fits the “many workers pulling crossmatch jobs” pattern.
- Easy to run as separate Deployments in Kubernetes.

### 6.2 Redis usage pattern
- Redis is used as:
  - Celery broker (`redis://redis:6379/0`)
  - (Optional) Celery result backend (`redis://redis:6379/1`) **or** disable results if not needed.

### 6.3 Delivery semantics
- Celery provides *at-least-once execution*; tasks can be re-delivered if workers crash.
- We rely on DB idempotency (UPSERT + unique constraints) to make retries safe.

### 6.4 Celery configuration recommendations
- `task_acks_late=True` (ack after work completes)
- `worker_prefetch_multiplier=1` (avoid long task hoarding)
- `task_reject_on_worker_lost=True`
- Per-task retry policy (e.g., `autoretry_for=(Exception,)`, `retry_backoff=True`, `max_retries=N`)

---

## 7. LSDB + Gaia Crossmatch Design

### 7.1 LSDB Crossmatch and Spatial Constraining

LSDB is designed to efficiently perform large-catalog queries and crossmatches by leveraging **HEALPix-partitioned catalogs, lazy evaluation, and Dask parallelism**. Instead of loading entire catalogs into memory, LSDB allows spatial constraints (e.g., cone or polygon filters) to be applied at load time so that only the relevant subset of a catalog is materialized for a given operation.

### Constraining the Search to a Small Cone or Patch

When crossmatching a specific alert against a large catalog like Gaia DR3, LSDB supports **spatial filters** that restrict the query to a region around the alert position. This approach enables efficient crossmatching without reading the full Gaia catalog.

For example, LSDB provides region-based search filters such as:

- **ConeSearch(ra, dec, radius_arcsec)** – limits the catalog to a circular cone on the sky
- **BoxSearch(ra, dec, …)** – limits to a bounding box
- **PolygonSearch(vertices)** – limits to an arbitrary polygon

These filters operate as a first-level constraint, so that only the HEALPix partitions overlapping the specified region are read and processed.

### Typical Crossmatch Workflow

A crossmatch operation in LSDB between an alert position and the Gaia catalog typically works as follows:

1. **Define a spatial filter** (e.g., a small cone around the alert RA/Dec with a configurable radius).
2. **Open the Gaia HATS catalog with the spatial filter** so that LSDB only loads the partitions intersecting the region of interest.
3. **Load only necessary columns** (e.g., `source_id`, `ra`, `dec`) to reduce I/O.
4. **Perform the crossmatch using the Dask-based LSDB API** — the internal algorithms handle partition-wise matching and join logic.

Here’s a minimal Python example (conceptually):

```python
import lsdb

# Define a small cone filter around the alert position
spatial_filter = lsdb.ConeSearch(alert_ra_deg, alert_dec_deg, radius_arcsec=MATCH_RADIUS)

# Open only the subset of the Gaia catalog covering the cone
gaia_subset = lsdb.open_catalog(
    GAIA_HATS_URL,
    columns=["source_id", "ra", "dec", "parallax", "phot_g_mean_mag"],
    search_filter=spatial_filter,
)

# Crossmatch against a small alert catalog or in-memory DataFrame
matches = gaia_subset.crossmatch(alert_catalog)
results = matches.compute()
```

LSDB builds an internal **Dask task graph** that only materializes the necessary data for the spatial region, performs the crossmatch on that subset, and returns the results. Because of HATS partitioning and the spatial filter, the number of Gaia rows read for each alert crossmatch can be kept very small (on the order of the local cone area rather than the whole sky).

### Margin Caches and Edge Effects

LSDB also supports **margin caches** — a mechanism to include additional overlap regions around HEALPix partitions so that objects just outside the nominal partition boundaries are not missed by a crossmatch. When performing a spatially constrained crossmatch, appropriate margin caches can be configured to avoid edge artifacts without loading the full catalog.

### Integration with HEROIC Constraints

In this architecture, HEROIC provides a **pointing region** (e.g., circular patch defined by its RA/Dec and FOV radius) for where Rubin is expected to observe. Workflows can combine this pointing information with an alert’s position/time to further refine the spatial constraint:

- Use the HEROIC-provided pointing (center + radius) as an input to the LSDB spatial filter (e.g., `lsdb.ConeSearch(heroic_ra, heroic_dec, heroic_radius)`).
- For each alert, adjust the filter to center on the alert’s coordinates with a crossmatch radius (configurable; e.g., a few arcseconds).

This combined filtering minimizes the Gaia HATS data that must be read and processed during each crossmatch task and dramatically accelerates worker throughput.

### 7.3 Match policy (initial)
- Store the best match (nearest neighbor) within a configurable radius.
- Recommended config:
  - `MATCH_RADIUS_ARCSEC` (e.g., 1.0–2.0 arcsec initially)
  - `N_NEIGHBORS=1`
- Tie-breaking: smallest separation; if equal, lowest Gaia source id.

---

## 8. Python Implementation

### 8.1 Runtime and libraries
- Python 3.11+

Core libraries:
- **Web/ORM framework:** **Django** (Django ORM + built-in migrations)
- **Queue:** `celery`, `redis`
- **DB driver:** `psycopg` (v3) via Django’s PostgreSQL backend
- **Config:** `pydantic-settings` (optional) or Django settings module with environment variable parsing (e.g., `django-environ`)
- **HTTP:** `httpx` (for HEROIC and for future LSST return)
- **Time conversions:** `astropy` (for MJD ↔ datetime as needed)
- **Observability:** `structlog`, `prometheus-client`, optional `opentelemetry-sdk`
- **LSDB:** `lsdb` Python APIs

Rationale:
- We use **Django** for the ORM and migrations to align with a potential future web UI that exposes system state.
- This also aligns with the SCiMMA **Blast** application’s established stack (Django + Celery + Redis + PostgreSQL), reducing operational and developer friction.

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
        sync_pointings.py
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
  heroic/
    __init__.py
    client.py
    schedule_sync.py
  matching/
    __init__.py
    gaia.py
    constraints.py
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
- **Celery beat** (optional) for periodic schedule refresh: `celery -A project beat`
- **Schedule sync worker** (alternative to beat): `python manage.py sync_pointings --loop`
- **Notifier**: `python manage.py run_notifier`

Database schema changes are managed with Django migrations:
- `python manage.py makemigrations`
- `python manage.py migrate`

### 8.4 Celery task definitions

- `tasks.crossmatch.crossmatch_alert(lsst_diaObject_diaObjectId: str, match_version: int = 1)`
- `tasks.schedule.refresh_planned_pointings()`

The schedule task should:
- Pull from HEROIC API
- Compute `radius_deg = s_fov_deg / 2`
- Replace the `planned_pointings` set in one transaction (delete+insert or upsert+prune)

---

## 9. Deployment

### 9.1 Kubernetes

Deployments (recommended):
- `ingest` Deployment (1–N replicas; ANTARES Kafka consumer)
- `lasair-ingest` Deployment (1 replica; Lasair Kafka consumer)
- `worker-crossmatch` Deployment (N replicas)
- `notifier` Deployment (1–2 replicas)
- `schedule-sync` (either Celery Beat or dedicated worker)

Dependencies:
- PostgreSQL (external managed or in-cluster)
- Redis (in-cluster)

#### 9.1.1 Container images
- Prefer **one service image** with multiple entrypoints/commands.
- All components run the same image tag (ensures reproducibility).

#### 9.1.2 Helm chart approach
We will create a top-level Helm chart `alertmatch` that deploys:
- Our services (ingest, workers, notifier, schedule)
- Optional dependency charts:
  - `redis`
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
- `CELERY_BROKER_URL=redis://redis:6379/0`
- `CELERY_RESULT_BACKEND=redis://redis:6379/1` (optional)
- `ANTARES_TOPIC=...`
- `HEROIC_BASE_URL=https://<heroic>/api`
- `GAIA_HATS_URL=https://...` or `s3://...`
- `MATCH_RADIUS_ARCSEC=...`
- `LASAIR_KAFKA_SERVER=kafka.lsst.ac.uk:9092`
- `LASAIR_TOPIC=lasair_<uid>_<filter-name>`
- `LASAIR_GROUP_ID=scimma-crossmatch-prod`

Secrets:
- DB password
- ANTARES credentials
- HEROIC credentials (if required)
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
- `redis`
- `ingest`
- `worker`
- `notifier`
- `scheduler` (celery beat or schedule-sync loop)

Notes:
- Use `.env` for configuration.
- If you want live code edits, either:
  - build a `:dev` image variant that mounts source, or
  - use `docker compose build` frequently.

---

## 10. Open Questions / Decisions Needed

1. **LSST return channel**: what mechanism do we implement first (HTTP endpoint? Kafka? Rubin-specific API)?
2. **ANTARES topic and auth**: exact configuration fields for `StreamingClient` (topic name, resume semantics).
3. **Match radius and columns**: what initial radius (arcsec) and which Gaia columns are needed in `gaia_payload`.
4. **Planned footprint gating**: do we skip crossmatch if alert is outside planned pointings, or just annotate?
5. **HEROIC API details**: exact endpoint path(s), pagination, auth, and any query params we can use for planned pointings.
6. ~~**Lasair Kafka auth**~~ — **Resolved**: `lasair_consumer` connects to `kafka.lsst.ac.uk:9092` without credentials. No SASL config or token required for the ingest path.
7. ~~**Lasair filter/topic**~~ — **Resolved**: filter `reliability_moderate` created on Lasair web UI; topic `lasair_366SCiMMA_reliability_moderate`. Criteria: `latestR > 0.7` AND `nDiaSources >= 1` AND last detection within 1 day. See §2.1 B2 for full SQL.
8. **Lasair alert schema**: what is the full JSON schema of a Lasair alert? Lasair uses `objectId` as the top-level key — confirm this is always identical to `lsst_diaObject_diaObjectId`. Confirm which field maps to LSST positional fields (RA/Dec).
9. **Lasair annotations to store**: which Lasair-side fields (Sherlock cross-matches, classification scores, etc.) should be preserved in `alert_deliveries.raw_payload`?

---

## 11. Appendices

### 11.1 HEROIC schedule record fields (reference)
HEROIC ingests the Rubin ObsLocTAP schedule and stores planned pointings derived from fields like:
- `s_ra`, `s_dec` (degrees)
- `s_fov` (degrees; diameter)
- `t_min`, `t_max` (MJD)

For LSSTCam typical values:
- `s_fov = 3.0°` → radius `1.5°` → `5400 arcsec`

### 11.2 Suggested first implementation milestone
- Ingest alert → store in DB → enqueue Celery task
- Crossmatch worker: match against Gaia with fixed radius
- Store match row
- Notifier: dummy implementation (logs payload) + `notifications` bookkeeping
- Schedule sync: refresh planned pointings table from HEROIC and have worker annotate “in planned footprint”

