---
title: "Implement ANTARES alert consumer"
type: feat
date: 2026-03-12
---

# Implement ANTARES Alert Consumer

## What We're Building

Replace the mock ANTARES alert consumer with a real consumer that connects to the
ANTARES alert broker using the `antares-client` Python library's `StreamingClient`.
The implementation parallels the existing Lasair consumer pattern: connect to a
Kafka-backed stream, normalize alerts to the canonical format, and ingest them
via the shared `ingest_alert()` pipeline.

Also rename the Docker service from `alert-consumer` to `antares-consumer` to
parallel `lasair-consumer`, and remove all mock alert functionality.

## Why This Approach

The `antares-client` library provides a `StreamingClient` that abstracts the Kafka
consumer setup, including authentication. Using `locus.properties` as the raw alert
dict allows reusing the existing `normalize_antares()` function with minimal changes,
since the properties dict contains the same flat `lsst_diaObject_*` / `ant_*` keys
as the mock alert.

## Key Decisions

- **Use `antares-client[subscriptions]`** — includes `confluent_kafka` dependency
  for streaming support. Add to `requirements.base.txt`.

- **Use `locus.properties` as raw alert** — the Locus `properties` dict contains
  the flat key-value pairs (`lsst_diaObject_diaObjectId`, `lsst_diaObject_ra`,
  `ant_time_received`, etc.) matching the existing `normalize_antares()` expectations.

- **Environment variables (parallel Lasair naming)**:
  - `ANTARES_API_KEY` — API key for authentication
  - `ANTARES_API_SECRET` — API secret for authentication
  - `ANTARES_TOPIC` — default: `lsst_scimma_quality_transient`
  - `ANTARES_GROUP_ID` — if empty, dynamically generated with timestamp suffix
    (dev mode); set via env var in production

- **Rename Docker service** — `alert-consumer` → `antares-consumer` in
  `docker-compose.yaml` to parallel `lasair-consumer`

- **Remove mock alert functionality** — delete `mock_alert_generator()` and
  replace with real `StreamingClient` consumer loop

- **Consumer pattern** — use `StreamingClient.iter()` for the poll loop
  (yields `(topic, locus)` tuples), wrapped in exponential backoff error handling
  matching the Lasair consumer pattern

- **StreamingClient constructor** — accepts `topics` (list), `api_key`, `api_secret`,
  and optional `group` kwarg (maps to Kafka `group.id`, defaults to hostname).
  Pass `ANTARES_GROUP_ID` as the `group` kwarg.

## Affected Files

| File | Change |
|------|--------|
| `crossmatch/brokers/antares/consumer.py` | **Replace** — real StreamingClient consumer |
| `crossmatch/requirements.base.txt` | **Add** `antares-client[subscriptions]` |
| `crossmatch/project/settings.py` | **Add** ANTARES env var config |
| `docker/docker-compose.yaml` | **Rename** service, add ANTARES env vars |
| `kubernetes/charts/crossmatch-service/values.yaml` | **Add** ANTARES config |

## Open Questions

None.
