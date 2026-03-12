---
title: "Publish crossmatch results to SCiMMA Hopskotch"
type: feat
date: 2026-03-12
---

# Publish Crossmatch Results to SCiMMA Hopskotch

## What We're Building

Implement the notifier subsystem to publish crossmatch results to the SCiMMA
Hopskotch Kafka service using the `hop-client` Python library. This is the first
concrete output channel for the crossmatch service — the LSST return channel
(design §4.4) remains TBD and will be added as a second destination later.

The notifier uses the existing `Notification` model and `watch_and_notify` stub
pattern. It polls for pending notifications and dispatches them to the
appropriate backend based on the `destination` field, supporting multiple
destinations from the start.

## Why This Approach

- **Hopskotch first**: We don't yet know the LSST return mechanism, but we can
  publish results to Hopskotch now for downstream consumers.
- **Notification model polling**: The `Notification` model already exists with
  `state`, `destination`, `payload`, and `attempts` fields. Using the polling
  pattern (via Celery Beat) fits the existing architecture — the batch dispatcher
  already works this way.
- **Multiple destinations**: Designing the notifier dispatcher to route by
  `destination` field means adding the LSST return channel later requires only a
  new backend implementation, not a notifier redesign.

## Key Decisions

1. **Publishing library**: `hop-client` PyPI package. Uses `Stream.open(url, "w")`
   to produce messages to Kafka. Supports plain dicts (auto-serialized as JSON).

2. **Trigger**: `crossmatch_batch` creates `Notification` rows (state=`pending`,
   destination=`hopskotch`) alongside `CatalogMatch` rows. A periodic Celery Beat
   task polls for pending notifications and publishes them.

3. **Runtime**: Celery Beat periodic task (not a standalone service). Fits the
   existing pattern used by the batch dispatcher.

4. **Authentication**: Environment variables (`HOPSKOTCH_USERNAME`,
   `HOPSKOTCH_PASSWORD`) passed to `hop.auth.Auth()`. Matches the ANTARES
   credential pattern.

5. **Broker URL and topic**: Separate environment variables matching the
   ANTARES/Lasair pattern. `HOPSKOTCH_BROKER_URL` (default
   `kafka://kafka.scimma.org`) and `HOPSKOTCH_TOPIC` (default TBD). The code
   constructs the full URL as `f"{HOPSKOTCH_BROKER_URL}/{HOPSKOTCH_TOPIC}"`.

6. **Message payload** (plain JSON dict):
   - `diaObjectId` — LSST DIA object identifier
   - `ra` — right ascension (degrees)
   - `dec` — declination (degrees)
   - `gaia_source_id` — matched Gaia DR3 source ID
   - `separation_arcsec` — angular separation of match

7. **Destination routing**: The notifier dispatcher reads `notification.destination`
   and calls the appropriate backend. Hopskotch is the first backend; others
   (e.g., `lsst-http`) can be added by registering a new handler.

8. **Design document**: Update §4.4 and add a new section for the Hopskotch
   publishing channel. §4.4 retains its TBD status for the LSST return mechanism.

## hop-client API Summary

```python
from hop import Stream
from hop.auth import Auth

auth = Auth(user=username, password=password)
stream = Stream(auth=auth)

url = f"{settings.HOPSKOTCH_BROKER_URL}/{settings.HOPSKOTCH_TOPIC}"
with stream.open(url, "w") as producer:
    producer.write({"diaObjectId": 123, "ra": 150.0, "dec": 2.0, ...})
```

- Package: `hop-client` on PyPI
- Source: https://github.com/scimma/hop-client
- Docs: https://hop-client.readthedocs.io/en/stable/
- URL format: `kafka://host:port/topic`
- Auth: SASL via `Auth(user, password)` or auto-loaded from `~/.config/hop/auth.toml`
- `write()` accepts plain dicts (serialized as JSON automatically)

## Open Questions

None — all questions resolved during brainstorming.
