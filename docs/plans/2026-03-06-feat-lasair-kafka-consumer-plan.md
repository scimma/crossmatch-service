---
title: "feat: Implement Lasair Kafka consumer"
type: feat
date: 2026-03-06
---

# ✨ feat: Implement Lasair Kafka consumer

## Overview

Replace the `NotImplementedError` stub in `brokers/lasair/consumer.py` with a
real Kafka consumer that connects to `lasair-lsst-kafka.lsst.ac.uk:9092`, polls the
`lasair_366SCiMMA_reliability_moderate` topic, normalizes alerts to the
internal LSST canonical schema, and gates ingestion through a shared two-step
atomic ingest helper that is also extracted from the ANTARES consumer.

No database migration is needed — `lsst_diaSource_diaSourceId` is already
`null=True` on the Alert model.

## Source

Brainstorm: `docs/brainstorms/2026-03-06-lasair-consumer-implementation-brainstorm.md`

## Technical Approach

### Architecture: shared ingest helper

Extract the two-step atomic ingest pattern (currently inline in
`brokers/antares/consumer.py`) into a shared `ingest_alert(canonical, broker)`
function in `brokers/__init__.py`. Both consumers call it after normalizing
their respective alert schemas.

```
brokers/
  __init__.py          ← ingest_alert(canonical, broker) goes here
  normalize.py         ← normalize_antares() + normalize_lasair()
  antares/
    consumer.py        ← refactored to call normalize_antares() + ingest_alert()
  lasair/
    consumer.py        ← NEW: real Kafka loop with exponential backoff
```

### Lasair alert schema → canonical dict mapping

Lasair Kafka messages contain the filter SQL columns. `normalize_lasair()` maps:

| Lasair field | Canonical field | Notes |
|---|---|---|
| `diaObjectId` | `lsst_diaObject_diaObjectId` | cast to `str` |
| `ra` | `ra_deg` | direct |
| `decl` | `dec_deg` | direct |
| `age` (float, days) | `event_time` | `utcnow() - timedelta(days=age)` |
| *(absent)* | `lsst_diaSource_diaSourceId` | `None` |
| *(whole msg)* | `payload` | stored raw |

### `lasair` package API (v0.1.2)

```python
from lasair import lasair_consumer as make_consumer
consumer = make_consumer(
    host=settings.LASAIR_KAFKA_SERVER,   # 'lasair-lsst-kafka.lsst.ac.uk:9092'
    group_id=settings.LASAIR_GROUP_ID,
    topic_in=settings.LASAIR_TOPIC,
)
msg = consumer.poll(timeout=20)   # None on timeout, message object otherwise
if msg is None: continue          # healthy connection, no messages
if msg.error(): raise ...         # Kafka-level error
data = json.loads(msg.value())
# Offsets auto-committed via group_id — no manual commit() needed
```

### Group ID strategy

| Environment | `LASAIR_GROUP_ID` value | Behavior |
|---|---|---|
| Dev (docker-compose) | unset / empty | Settings generates `scimma-crossmatch-dev-{timestamp}` → each restart replays |
| Prod (k8s) | `scimma-crossmatch-prod` | Stable → resumes from last committed offset |

### Exponential backoff

On exception (including `msg.error()`): sleep starting at 1 s, doubling up
to 60 s. Reset to 1 s on any successful poll iteration (message or timeout).

## Implementation Plan

### Phase 1: Requirements & settings

- [x] Add `lasair` to `crossmatch/requirements.base.txt`
- [x] Add Lasair settings block to `crossmatch/project/settings.py`:
  ```python
  # Lasair Kafka consumer
  import time as _time
  _lasair_group_id = os.environ.get('LASAIR_GROUP_ID', '')
  LASAIR_KAFKA_SERVER = os.environ.get('LASAIR_KAFKA_SERVER', 'lasair-lsst-kafka.lsst.ac.uk:9092')
  LASAIR_TOPIC = os.environ.get('LASAIR_TOPIC', 'lasair_366SCiMMA_reliability_moderate')
  LASAIR_GROUP_ID = _lasair_group_id or f'scimma-crossmatch-dev-{int(_time.time())}'
  ```

### Phase 2: Normalization layer (`brokers/normalize.py`)

- [x] Implement `normalize_antares(raw_alert: dict) -> dict`
  - Maps `lsst_diaObject_diaObjectId`, `lsst_diaObject_ra/dec`, `ant_time_received`,
    `lsst_diaSource_diaSourceId` to canonical keys
  - `event_time = datetime.fromtimestamp(raw['ant_time_received'], tz=timezone.utc)`
- [x] Implement `normalize_lasair(raw_alert: dict) -> dict`
  - Maps `diaObjectId`, `ra`, `decl`, derives `event_time` from `age`
  - Sets `lsst_diaSource_diaSourceId = None`
  - `event_time = datetime.now(tz=timezone.utc) - timedelta(days=raw['age'])`

```python
# brokers/normalize.py
from datetime import datetime, timedelta, timezone


def normalize_antares(raw_alert: dict) -> dict:
    """Normalize an ANTARES alert to the internal canonical format."""
    return {
        'lsst_diaObject_diaObjectId': str(raw_alert['lsst_diaObject_diaObjectId']),
        'ra_deg': raw_alert['lsst_diaObject_ra'],
        'dec_deg': raw_alert['lsst_diaObject_dec'],
        'lsst_diaSource_diaSourceId': str(raw_alert['lsst_diaSource_diaSourceId']),
        'event_time': datetime.fromtimestamp(raw_alert['ant_time_received'], tz=timezone.utc),
        'payload': raw_alert,
    }


def normalize_lasair(raw_alert: dict) -> dict:
    """Normalize a Lasair alert to the internal canonical format."""
    return {
        'lsst_diaObject_diaObjectId': str(raw_alert['diaObjectId']),
        'ra_deg': raw_alert['ra'],
        'dec_deg': raw_alert['decl'],
        'lsst_diaSource_diaSourceId': None,
        'event_time': datetime.now(tz=timezone.utc) - timedelta(days=raw_alert['age']),
        'payload': raw_alert,
    }
```

### Phase 3: Shared ingest helper (`brokers/__init__.py`)

- [x] Implement `ingest_alert(canonical: dict, broker: str) -> bool`:
  - Step 1: `Alert.objects.get_or_create(lsst_diaObject_diaObjectId=..., defaults={...})`
  - Step 2: `AlertDelivery.objects.get_or_create(alert=..., broker=...)`
  - If `created` is False: log and return `False`
  - If `created` is True: dispatch `crossmatch_alert.delay(...)`, return `True`

```python
# brokers/__init__.py
from tasks.crossmatch import crossmatch_alert
from core.models import Alert, AlertDelivery
from core.log import get_logger

logger = get_logger(__name__)


def ingest_alert(canonical: dict, broker: str) -> bool:
    """Two-step atomic ingest gate (§5.3). Returns True if task was dispatched."""
    alert_id = canonical['lsst_diaObject_diaObjectId']
    alert_obj, _ = Alert.objects.get_or_create(
        lsst_diaObject_diaObjectId=alert_id,
        defaults=dict(
            ra_deg=canonical['ra_deg'],
            dec_deg=canonical['dec_deg'],
            lsst_diaSource_diaSourceId=canonical.get('lsst_diaSource_diaSourceId'),
            event_time=canonical['event_time'],
            payload=canonical['payload'],
            status=Alert.Status.INGESTED,
        ),
    )
    _, created = AlertDelivery.objects.get_or_create(
        alert=alert_obj,
        broker=broker,
    )
    if not created:
        logger.info('alert already delivered, skipping', alert_id=alert_id, broker=broker)
        return False
    logger.info(f'New alert ingested: {alert_obj}')
    crossmatch_alert.delay(lsst_diaObject_diaObjectId=alert_obj.lsst_diaObject_diaObjectId)
    return True
```

### Phase 4: Refactor ANTARES consumer (`brokers/antares/consumer.py`)

- [x] Replace inline two-step ingest logic with call to `normalize_antares()` + `ingest_alert()`
- [x] Keep `mock_alert_generator()` unchanged (still used for dev)
- [x] Keep `sleep(randint(5, 15))` between polls (mock-only; Lasair uses `poll(timeout=)`)

```python
# brokers/antares/consumer.py  (key loop body, rest unchanged)
from brokers import ingest_alert
from brokers.normalize import normalize_antares

def consume_alerts():
    logger.info('Listening to alert broker...')
    while True:
        try:
            raw = mock_alert_generator()
            canonical = normalize_antares(raw)
            ingest_alert(canonical, broker=BROKER_NAME)
        except Exception as err:
            logger.error(f'Error ingesting alert: {err}')
        sleep(randint(5, 15))
```

### Phase 5: Lasair consumer (`brokers/lasair/consumer.py`)

- [x] Connect to Kafka using `lasair_consumer(host, group_id, topic_in)`
- [x] Poll loop: `None` → continue; `msg.error()` → raise; else normalize + ingest
- [x] Exponential backoff on exception: start 1 s, double, cap 60 s, reset on success

```python
# brokers/lasair/consumer.py
import json
import time
from django.conf import settings
from lasair import lasair_consumer as make_consumer
from brokers import ingest_alert
from brokers.normalize import normalize_lasair
from core.log import get_logger

logger = get_logger(__name__)
BROKER_NAME = 'lasair'
_BACKOFF_INITIAL = 1
_BACKOFF_MAX = 60


def consume_alerts():
    logger.info(
        'Connecting to Lasair Kafka broker...',
        host=settings.LASAIR_KAFKA_SERVER,
        topic=settings.LASAIR_TOPIC,
        group_id=settings.LASAIR_GROUP_ID,
    )
    consumer = make_consumer(
        host=settings.LASAIR_KAFKA_SERVER,
        group_id=settings.LASAIR_GROUP_ID,
        topic_in=settings.LASAIR_TOPIC,
    )
    backoff = _BACKOFF_INITIAL
    logger.info('Listening for Lasair alerts...')
    while True:
        try:
            msg = consumer.poll(timeout=20)
            if msg is None:
                backoff = _BACKOFF_INITIAL   # connection healthy
                continue
            if msg.error():
                raise Exception(f'Kafka error: {msg.error()}')
            raw = json.loads(msg.value())
            canonical = normalize_lasair(raw)
            ingest_alert(canonical, broker=BROKER_NAME)
            backoff = _BACKOFF_INITIAL
        except Exception as err:
            logger.error(f'Error consuming Lasair alert: {err}')
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
```

### Phase 6: Infrastructure wiring

#### `docker/docker-compose.yaml`

- [x] Replace the three commented-out Lasair vars with active env vars using the
  combined `LASAIR_KAFKA_SERVER` name (matches `settings.py` and `lasair_consumer(host=...)`):
  ```yaml
  LASAIR_KAFKA_SERVER: "${LASAIR_KAFKA_SERVER:-lasair-lsst-kafka.lsst.ac.uk:9092}"
  LASAIR_TOPIC: "${LASAIR_TOPIC:-lasair_366SCiMMA_reliability_moderate}"
  LASAIR_GROUP_ID: "${LASAIR_GROUP_ID:-}"   # empty → settings.py generates timestamp suffix
  ```

#### `kubernetes/charts/crossmatch-service/templates/statefulset.yaml`

- [x] Add three Lasair env vars inline in the lasair-consumer StatefulSet container spec,
  after the existing `{{- include "celery.env" ... }}` line:
  ```yaml
          - name: LASAIR_KAFKA_SERVER
            value: {{ .Values.lasair_consumer.kafka_server | quote }}
          - name: LASAIR_TOPIC
            value: {{ .Values.lasair_consumer.topic | quote }}
          - name: LASAIR_GROUP_ID
            value: {{ .Values.lasair_consumer.group_id | quote }}
  ```

#### `kubernetes/charts/crossmatch-service/values.yaml`

- [x] Add `kafka_server`, `topic`, `group_id` to the existing `lasair_consumer` block:
  ```yaml
  lasair_consumer:
    enabled: false
    replicaCount: 1
    service:
      host: "lasair-consumer"
    kafka_server: "lasair-lsst-kafka.lsst.ac.uk:9092"
    topic: "lasair_366SCiMMA_reliability_moderate"
    group_id: "scimma-crossmatch-prod"
    resources:
      requests:
        cpu: '500m'
        memory: 100Mi
      limits:
        cpu: '2'
        memory: 2Gi
  ```

## Acceptance Criteria

- [ ] `lasair` package is in `requirements.base.txt`
- [ ] `settings.py` exposes `LASAIR_KAFKA_SERVER`, `LASAIR_TOPIC`, `LASAIR_GROUP_ID`
- [ ] `LASAIR_GROUP_ID` defaults to a timestamp-suffixed dev value when env var is unset
- [ ] `normalize_antares()` and `normalize_lasair()` are implemented (no `NotImplementedError`)
- [ ] `ingest_alert(canonical, broker)` is implemented in `brokers/__init__.py`
- [ ] ANTARES consumer calls `normalize_antares()` + `ingest_alert()` (no inline two-step)
- [ ] Lasair consumer connects to real Kafka and runs the poll loop
- [ ] Lasair consumer applies exponential backoff (1s → 60s) on exceptions
- [ ] `msg.error()` is checked and treated as an exception
- [ ] `docker-compose.yaml` has active (uncommented) Lasair env vars with `LASAIR_KAFKA_SERVER`
- [ ] `statefulset.yaml` injects `LASAIR_KAFKA_SERVER`, `LASAIR_TOPIC`, `LASAIR_GROUP_ID`
- [ ] `values.yaml` has `kafka_server`, `topic`, `group_id` under `lasair_consumer`
- [ ] No `NotImplementedError` remains in the broker code paths

## Dependencies & Risks

**Kafka auth validation**: `lasair_consumer` (Kafka) is confirmed to require no
credentials, but this must be validated by running the consumer against the live
topic in a dev environment before enabling the k8s pod (`enabled: false` stays
until confirmed).

**`normalize_antares()` touches the ANTARES path**: Refactoring the ANTARES
consumer to use `normalize_antares()` + `ingest_alert()` is a behavior-neutral
refactor, but it changes code that is currently running in dev. Test manually
after the change by verifying mock alerts continue to be ingested and tasks
dispatched.

**`lasair` package import namespace**: The package is `lasair` on PyPI and
imports as `from lasair import lasair_consumer`. Confirm no collision with other
packages.

## Files Changed

| File | Type | Change |
|------|------|--------|
| `crossmatch/requirements.base.txt` | modified | Add `lasair` |
| `crossmatch/project/settings.py` | modified | Add LASAIR_* settings block |
| `crossmatch/brokers/__init__.py` | modified | Add `ingest_alert()` |
| `crossmatch/brokers/normalize.py` | modified | Implement both normalizers |
| `crossmatch/brokers/antares/consumer.py` | modified | Refactor to use shared helper |
| `crossmatch/brokers/lasair/consumer.py` | modified | Real Kafka implementation |
| `docker/docker-compose.yaml` | modified | Uncomment/rename Lasair env vars |
| `kubernetes/.../templates/statefulset.yaml` | modified | Add Lasair env vars to lasair-consumer pod |
| `kubernetes/.../values.yaml` | modified | Add kafka_server, topic, group_id |

**No model migration required** — `lsst_diaSource_diaSourceId` is already
`null=True` in the Alert model.

## References

- Brainstorm: `docs/brainstorms/2026-03-06-lasair-consumer-implementation-brainstorm.md`
- Kafka/filter decisions: `docs/brainstorms/2026-03-06-lasair-filter-and-auth-brainstorm.md`
- Design doc §4.5: Lasair Kafka interface
- Design doc §5.3: Atomic delivery gate pattern
- `lasair` PyPI package v0.1.2: https://pypi.org/project/lasair/
- ANTARES consumer (pattern reference): `crossmatch/brokers/antares/consumer.py`
