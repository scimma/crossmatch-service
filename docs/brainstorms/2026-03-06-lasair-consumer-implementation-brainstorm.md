---
title: Lasair Consumer Implementation
date: 2026-03-06
topic: lasair-consumer
tags: [lasair, consumer, kafka, brokers, ingest]
---

# Lasair Consumer Implementation

## What We're Building

A real Lasair Kafka consumer that replaces the `NotImplementedError` stub in
`crossmatch/brokers/lasair/consumer.py`. The consumer connects to
`lasair-lsst-kafka.lsst.ac.uk:9092`, polls the `lasair_366SCiMMA_reliability_moderate`
topic using the `lasair` PyPI package, normalizes incoming alerts to the
internal LSST canonical schema, and gates ingestion through the shared two-step
atomic ingest helper.

Alongside the consumer itself: a shared `ingest_alert()` helper extracted into
`brokers/__init__.py`, implementation of `normalize_lasair()` in
`brokers/normalize.py`, a migration making `lsst_diaSource_diaSourceId`
nullable, `lasair` added to requirements, and Lasair env vars wired through
docker-compose and Kubernetes.

## Why This Approach

**Shared ingest helper (`brokers/__init__.py`)**: Rather than duplicating the
`Alert.get_or_create + AlertDelivery.get_or_create + crossmatch_alert.delay`
pattern in both consumer modules, extract it once. Both ANTARES and Lasair
consumers call `ingest_alert(canonical_alert, broker='antares'|'lasair')`.
The ANTARES consumer is refactored to use it at the same time.

**Real Kafka via `lasair` package**: The design document (§4.5) specifies
`lasair_consumer()` from the `lasair` PyPI package. No auth required.

**Exponential backoff**: Preferred over crash-and-restart or flat sleep for
connection errors. Poll timeouts (no messages in 20s window) are normal and
loop silently. Exceptions trigger backoff starting at 1s, doubling up to 60s,
resetting on success.

## Key Decisions

### Lasair alert schema
Kafka messages contain exactly the filter SQL columns:
- `diaObjectId` → `lsst_diaObject_diaObjectId`
- `ra` → `ra_deg`
- `decl` → `dec_deg`
- `nDiaSources`, `latestR`, `age` → stored in `payload` as-is

### event_time derivation
Lasair provides `age` (days since last detection, float). Derive event_time as:
```python
event_time = datetime.utcnow() - timedelta(days=alert['age'])
```
This is approximate (age is a snapshot from filter time) but sufficient.

### lsst_diaSource_diaSourceId
Lasair does not provide a diaSource ID. Store `null`. Requires:
- `lsst_diaSource_diaSourceId = models.TextField(null=True, ...)` in `Alert`
- New migration `0002_alert_diasourceid_nullable.py`

### Group ID strategy
Controlled by `LASAIR_GROUP_ID` env var.
- **Prod**: stable value (e.g. `scimma-crossmatch-prod`) — resumes from last
  committed offset across restarts.
- **Dev docker-compose**: timestamp-suffixed default
  (e.g. `scimma-crossmatch-dev-{int(time.time())}`) so each restart replays
  from the latest available offset without needing manual offset reset.

### normalize_lasair() location
Implemented in `brokers/normalize.py` alongside the existing `normalize_antares()`
stub. Returns a canonical dict:
```python
{
    'lsst_diaObject_diaObjectId': str(alert['diaObjectId']),
    'ra_deg': alert['ra'],
    'dec_deg': alert['decl'],
    'lsst_diaSource_diaSourceId': None,
    'event_time': datetime.utcnow() - timedelta(days=alert['age']),
    'payload': alert,      # raw Lasair message stored in full
}
```

## Files Affected

| File | Change |
|------|--------|
| `crossmatch/brokers/__init__.py` | Add `ingest_alert(canonical, broker)` shared helper |
| `crossmatch/brokers/antares/consumer.py` | Refactor to call `ingest_alert()` |
| `crossmatch/brokers/lasair/consumer.py` | Real implementation |
| `crossmatch/brokers/normalize.py` | Implement `normalize_lasair()` |
| `crossmatch/core/models.py` | Make `lsst_diaSource_diaSourceId` nullable |
| `crossmatch/core/migrations/0002_alert_diasourceid_nullable.py` | New migration |
| `crossmatch/requirements.base.txt` | Add `lasair` |
| `docker/docker-compose.yaml` | Uncomment + add Lasair env vars |
| `kubernetes/charts/crossmatch-service/templates/statefulset.yaml` | Add Lasair env var block |
| `kubernetes/charts/crossmatch-service/templates/_helpers.tpl` | Add `lasair.env` helper (or inline) |

## `lasair` Package API (v0.1.2, resolved)

**Constructor** (parameter names matter for keyword calls):
```python
from lasair import lasair_consumer
consumer = lasair_consumer(
    host=settings.LASAIR_KAFKA_SERVER,   # 'lasair-lsst-kafka.lsst.ac.uk:9092'
    group_id=settings.LASAIR_GROUP_ID,
    topic_in=settings.LASAIR_TOPIC,      # 'lasair_366SCiMMA_reliability_moderate'
)
```

**Poll loop** — `poll()` returns `None` on timeout (no exception). Non-None
messages may still carry a Kafka error via `msg.error()`:
```python
msg = consumer.poll(timeout=20)
if msg is None:
    continue            # normal: no messages in window
if msg.error():
    raise KafkaError(str(msg.error()))
alert_data = json.loads(msg.value())
```

**Offset commit**: Auto-managed by the library via `group_id`. No manual
`commit()` call needed.

**Backoff scope**: Increment on exception (including `msg.error()`). Reset on
any successful poll iteration (message or timeout — both mean the connection
is healthy).

## Open Questions

1. **Kafka consumer auth**: `lasair_client` (REST API) requires an API token,
   but previous research confirmed `lasair_consumer` (Kafka) requires no auth.
   Confirm by running the consumer against the live topic in dev before
   enabling the k8s pod.

**Resolved:**
- **Kubernetes env**: Inline `LASAIR_KAFKA_SERVER`, `LASAIR_TOPIC`,
  `LASAIR_GROUP_ID` directly in the lasair-consumer StatefulSet block.
  No new `_helpers.tpl` partial.
- **`normalize_antares()`**: Implement alongside `normalize_lasair()` so both
  broker paths use the normalize layer and the shared ingest helper is
  exercised end-to-end from both consumers.
