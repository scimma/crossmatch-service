---
date: 2026-04-03
topic: add-pittgoogle-alert-consumer
---

# Add Pitt-Google Alert Consumer

## Problem Frame

The crossmatch-service currently ingests LSST alerts from two brokers: ANTARES (antares-client StreamingClient) and Lasair (Kafka consumer). Pitt-Google is a third LSST community broker that serves alerts via Google Cloud Pub/Sub. Adding it increases alert coverage and provides broker redundancy. The existing multi-broker architecture (shared `ingest_alert()` with idempotent deduplication via AlertDelivery) already supports additional brokers as a config+code addition.

## Requirements

- R1. Add a `pittgoogle` consumer module at `crossmatch/brokers/pittgoogle/consumer.py` that subscribes to the Pitt-Google `lsst-alerts` topic using `pittgoogle.pubsub.Consumer.stream()` and ingests alerts through the shared `ingest_alert()` pipeline
- R2. Add a `normalize_pittgoogle()` function to `crossmatch/brokers/normalize.py` that maps the `pittgoogle.Alert` object to the canonical format (`lsst_diaObject_diaObjectId`, `ra_deg`, `dec_deg`, `lsst_diaSource_diaSourceId`, `event_time`, `payload`)
- R3. Create the Pub/Sub subscription with attribute filter `attributes:diaObject_diaObjectId` to drop alerts without a diaObjectId server-side (e.g., solar system objects)
- R4. Authenticate to Google Cloud using `GOOGLE_CLOUD_PROJECT` env var and `GOOGLE_APPLICATION_CREDENTIALS` env var pointing to a mounted service account JSON key file
- R5. Add `pittgoogle-client` to `requirements.base.txt`
- R6. Add a `run_pittgoogle_ingest` Django management command and entrypoint script, following the ANTARES/Lasair pattern
- R7. Add a `pittgoogle-consumer` service to `docker/docker-compose.yaml` with all required environment variables
- R8. Add a `pittgoogle_consumer` section to the Helm chart (`values.yaml`, `_helpers.yaml`, `statefulset.yaml`) with `enabled` toggle, GCP credential Secret volume mount, and configurable topic/subscription/project settings
- R9. Add Pitt-Google configuration variables to `docker/.env.example` and `kubernetes/dev-overrides.yaml.example`
- R10. Update the design document to list Pitt-Google as an active broker

## Success Criteria

- Alerts published to the Pitt-Google `lsst-alerts` topic produce `Alert` and `AlertDelivery` rows with `broker='pittgoogle'`
- Alerts already ingested from ANTARES or Lasair are deduplicated (same `lsst_diaObject_diaObjectId`, new AlertDelivery only)
- Consumer handles message callback errors (e.g., database failures) by returning `Response(ack=False)` so Pub/Sub redelivers the message; Pub/Sub connection management is handled internally by the Google client library
- GCP credentials are never stored in code, env files, or Helm values — only via mounted Secrets

## Scope Boundaries

- No changes to the crossmatch or notification pipeline — consumer feeds the existing ingest path
- Subscription creation (R3) happens at consumer startup via `subscription.touch()`, not as a separate provisioning step
- No Workload Identity support in this iteration — service account JSON key file only
- No batch_callback usage — all processing happens in the per-message callback

## Key Decisions

- **Topic: `lsst-alerts`** — Full Avro-serialized LSST alerts in Pitt-Google project `pitt-alert-broker`. Most complete data, matching what ANTARES/Lasair receive.
- **Consumer pattern: `pittgoogle.pubsub.Consumer.stream()`** — Callback-based, blocks indefinitely. Different from the polling loops used by ANTARES/Lasair but achieves the same result (long-running process that ingests alerts).
- **Server-side attribute filter** — `attributes:diaObject_diaObjectId` drops alerts without a diaObjectId before they reach the consumer, reducing unnecessary processing. Filter is immutable once set on the subscription.
- **GCP auth: env vars + mounted key file** — `GOOGLE_CLOUD_PROJECT` and `GOOGLE_APPLICATION_CREDENTIALS` env vars, with the JSON key file mounted as a K8s Secret volume. Standard GCP pattern.
- **Normalization via Alert object properties** — The `pittgoogle.Alert` object exposes `.objectid`, `.sourceid`, `.ra`, `.dec`, `.dict` directly, simplifying normalization compared to ANTARES/Lasair raw dicts.

## Dependencies / Assumptions

- A Google Cloud project with the Pub/Sub API enabled is available for creating the subscription
- A GCP service account with permissions to subscribe to Pitt-Google topics has been provisioned
- The `pittgoogle-client` package is compatible with the current Python 3.12 and dependency pins (lsdb==0.8.1, numpy==2.4.2, pandas==2.3.3)

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Needs research] What timestamp field does the `pittgoogle.Alert` object expose for `event_time`? May need to inspect `.attributes` or `.dict` keys.
- [Affects R5][Needs research] Confirm `pittgoogle-client` resolves cleanly with existing pinned dependencies (Python 3.12, numpy, pandas, etc.)
- [Affects R8][Technical] What is the appropriate K8s Secret volume mount path for the GCP key file? (e.g., `/var/run/secrets/gcp/key.json`)
- [Affects R3][Technical] What should the default subscription name be? Likely `scimma-crossmatch-lsst-alerts` or similar, configurable via env var.

## Next Steps

→ `/ce:plan` for structured implementation planning
