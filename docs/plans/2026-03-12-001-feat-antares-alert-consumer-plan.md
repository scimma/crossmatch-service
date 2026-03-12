---
title: "Implement ANTARES alert consumer"
type: feat
status: active
date: 2026-03-12
origin: docs/brainstorms/2026-03-12-implement-antares-consumer-brainstorm.md
---

# Implement ANTARES Alert Consumer

## Overview

Replace the mock ANTARES alert consumer with a real consumer using the
`antares-client` library's `StreamingClient`. The implementation parallels the
existing Lasair consumer: connect to a Kafka-backed stream, normalize alerts to
canonical format, and ingest via the shared `ingest_alert()` pipeline. Rename
the Docker service from `alert-consumer` to `antares-consumer` and remove all
mock alert code.

## Proposed Solution

### 1. Add `antares-client[subscriptions]` to requirements

Add to `crossmatch/requirements.base.txt`. The `[subscriptions]` extra pulls in
`confluent_kafka` for streaming support.

### 2. Add ANTARES settings to `crossmatch/project/settings.py`

```python
# ANTARES broker configuration
ANTARES_API_KEY = os.environ.get('ANTARES_API_KEY', '')
ANTARES_API_SECRET = os.environ.get('ANTARES_API_SECRET', '')
ANTARES_TOPIC = os.environ.get('ANTARES_TOPIC', 'lsst_scimma_quality_transient')

_antares_group_id = os.environ.get('ANTARES_GROUP_ID', '')
if not _antares_group_id:
    import time as _time
    _antares_group_id = f'scimma-crossmatch-dev-{int(_time.time())}'
ANTARES_GROUP_ID = _antares_group_id
```

Pattern matches existing Lasair config at `settings.py:120-131`.

### 3. Replace `crossmatch/brokers/antares/consumer.py`

Replace mock consumer with real `StreamingClient` consumer loop:

```python
from antares_client import StreamingClient
from django.conf import settings

client = StreamingClient(
    topics=[settings.ANTARES_TOPIC],
    api_key=settings.ANTARES_API_KEY,
    api_secret=settings.ANTARES_API_SECRET,
    group=settings.ANTARES_GROUP_ID,
)

for topic, locus in client.iter():
    raw = locus.properties
    canonical = normalize_antares(raw)
    ingest_alert(canonical, broker=BROKER_NAME)
```

Wrap in exponential backoff error handling. Note: `StreamingClient.iter()` is a
high-level iterator (unlike Lasair's raw `consumer.poll()` + `msg.error()` pattern),
so the try/except wraps the `iter()` loop body, not individual poll calls. On
exception, sleep with backoff, then reconnect by creating a new `StreamingClient`.

Key points:
- `StreamingClient` constructor: `topics` (list), `api_key`, `api_secret`,
  `group` kwarg (maps to Kafka `group.id`)
  (see brainstorm: `docs/brainstorms/2026-03-12-implement-antares-consumer-brainstorm.md`)
- `locus.properties` contains flat `lsst_diaObject_*` / `ant_*` keys matching
  what `normalize_antares()` already expects — no changes needed to normalizer
- Delete `mock_alert_generator()` entirely
- `brokers/antares/publisher.py` is empty — leave as-is (placeholder for future use)

### 4. Rename Docker service in `docker/docker-compose.yaml`

- Rename `alert-consumer` service to `antares-consumer`
- Add ANTARES environment variables: `ANTARES_API_KEY`, `ANTARES_API_SECRET`,
  `ANTARES_TOPIC`, `ANTARES_GROUP_ID`
- Keep same entrypoint: `bash entrypoints/run_antares_ingest.sh`

### 5. Add ANTARES config to Kubernetes values

Add to `kubernetes/charts/crossmatch-service/values.yaml`:

```yaml
antares:
  api_key: ""
  api_secret: ""
  topic: "lsst_scimma_quality_transient"
  group_id: ""
```

## Acceptance Criteria

- [x] `antares-client[subscriptions]` added to `requirements.base.txt`
- [x] ANTARES env vars added to `settings.py` (API_KEY, API_SECRET, TOPIC, GROUP_ID)
- [x] `brokers/antares/consumer.py` uses real `StreamingClient` with `iter()` loop
- [x] `mock_alert_generator()` removed entirely
- [x] `normalize_antares()` works with `locus.properties` dict (no changes expected)
- [x] Docker service renamed from `alert-consumer` to `antares-consumer`
- [x] ANTARES env vars added to `docker-compose.yaml` antares-consumer service
- [x] Kubernetes `values.yaml` updated with ANTARES config
- [x] Consumer logs connection info on startup (topic, group_id)
- [x] Exponential backoff on errors matching Lasair consumer pattern

## References

- **Origin brainstorm:** `docs/brainstorms/2026-03-12-implement-antares-consumer-brainstorm.md`
  - Key decisions: use `antares-client[subscriptions]`, `locus.properties` as raw alert,
    `group` kwarg for consumer group ID
- Lasair consumer (pattern to follow): `crossmatch/brokers/lasair/consumer.py`
- ANTARES normalizer: `crossmatch/brokers/normalize.py:10-23`
- Shared ingest pipeline: `crossmatch/brokers/__init__.py`
- ANTARES client docs: https://nsf-noirlab.gitlab.io/csdc/antares/client
- ANTARES client source: https://gitlab.com/nsf-noirlab/csdc/antares/client
