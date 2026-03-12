---
title: "Publish crossmatch results to SCiMMA Hopskotch"
type: feat
status: completed
date: 2026-03-12
origin: docs/brainstorms/2026-03-12-publish-crossmatch-to-hopskotch-brainstorm.md
---

# Publish Crossmatch Results to SCiMMA Hopskotch

## Overview

Implement the notifier subsystem to publish crossmatch results to the SCiMMA
Hopskotch Kafka service using `hop-client`. The crossmatch_batch task creates
`Notification` rows after writing `CatalogMatch` rows. A periodic Celery Beat
task polls for pending notifications and dispatches them to destination-specific
backends. Hopskotch is the first backend; the LSST return channel (§4.4) remains
TBD and will be added as a second destination later.

## Proposed Solution

### 1. Add `hop-client` to requirements

Add to `crossmatch/requirements.base.txt`.

### 2. Add Hopskotch settings to `crossmatch/project/settings.py`

```python
######################################################################
# SCiMMA Hopskotch publisher
#
HOPSKOTCH_BROKER_URL = os.environ.get('HOPSKOTCH_BROKER_URL', 'kafka://kafka.scimma.org')
HOPSKOTCH_TOPIC = os.environ.get('HOPSKOTCH_TOPIC', '')
HOPSKOTCH_USERNAME = os.environ.get('HOPSKOTCH_USERNAME', '')
HOPSKOTCH_PASSWORD = os.environ.get('HOPSKOTCH_PASSWORD', '')
```

Pattern matches existing ANTARES/Lasair config at `settings.py:122-143`.

### 3. Create Notification rows in `crossmatch_batch` (`tasks/crossmatch.py`)

After `CatalogMatch.objects.bulk_create()` (line 55), create `Notification` rows
for each match with `destination='hopskotch'`, `state='pending'`, and payload
containing the five agreed fields.

**`bulk_create` PK issue**: `bulk_create(ignore_conflicts=True)` does not return
auto-generated PKs on PostgreSQL. To populate `Notification.catalog_match` FK:
- Query back the just-created `CatalogMatch` rows by `(alert_id, catalog_name,
  match_version)` — the unique constraint guarantees these exist.
- Or set `catalog_match=None` (the FK is nullable). Simpler, and the Notification
  payload already contains all needed fields.

**Recommended**: Set `catalog_match=None` for simplicity. The payload is
self-contained.

Payload format (see brainstorm: `docs/brainstorms/2026-03-12-publish-crossmatch-to-hopskotch-brainstorm.md`):

```python
{
    "diaObjectId": alert.lsst_diaObject_diaObjectId,
    "ra": alert.ra_deg,
    "dec": alert.dec_deg,
    "gaia_source_id": match.catalog_source_id,
    "separation_arcsec": match.match_distance_arcsec,
}
```

**Alerts with no Gaia match**: No `CatalogMatch` row is created, so no
`Notification` row is created. These alerts still transition to `MATCHED` (they
were matched — just with zero results). They skip the `NOTIFIED` state since
there is nothing to notify.

### 4. Implement notifier dispatch task (`tasks/schedule.py`)

Add a `DispatchNotifications` descriptor and `dispatch_notifications` shared task
following the `DispatchCrossmatchBatch` pattern:

```python
class DispatchNotifications:
    task_name = 'Dispatch Notifications'
    task_handle = 'dispatch_notifications'
    task_frequency_seconds = 10
    task_initially_enabled = True


@shared_task
def dispatch_notifications() -> None:
    """Poll for pending notifications and dispatch to destination backends.

    Holds select_for_update lock during publishing to prevent concurrent
    Beat ticks from picking up the same rows.
    """
    from django.db import transaction
    from core.models import Notification

    with transaction.atomic():
        pending = (
            Notification.objects.filter(state=Notification.State.PENDING)
            .select_for_update(skip_locked=True)
            .order_by('created_at')
            [:500]
        )
        pending_list = list(pending)

        if not pending_list:
            return

        # Group by destination and dispatch within the transaction
        by_dest = {}
        for notif in pending_list:
            by_dest.setdefault(notif.destination, []).append(notif)

        for destination, notifications in by_dest.items():
            handler = DESTINATION_HANDLERS.get(destination)
            if handler is None:
                logger.error('Unknown notification destination', destination=destination)
                continue
            handler(notifications)
```

### 5. Implement destination handler registry and Hopskotch backend

Create `notifier/dispatch.py` with the handler registry:

```python
DESTINATION_HANDLERS = {
    'hopskotch': send_hopskotch_batch,
}
```

Create `notifier/impl_hopskotch.py` with the Hopskotch backend:

```python
from hop import Stream
from hop.auth import Auth
from django.conf import settings
from django.utils import timezone
from core.log import get_logger

logger = get_logger(__name__)


def send_hopskotch_batch(notifications):
    """Publish a batch of notifications to Hopskotch via hop-client."""
    url = f"{settings.HOPSKOTCH_BROKER_URL}/{settings.HOPSKOTCH_TOPIC}"
    auth = Auth(user=settings.HOPSKOTCH_USERNAME, password=settings.HOPSKOTCH_PASSWORD)
    stream = Stream(auth=auth)

    with stream.open(url, "w") as producer:
        for notif in notifications:
            try:
                producer.write(notif.payload)
                notif.state = Notification.State.SENT
                notif.sent_at = timezone.now()
                notif.attempts += 1
                notif.save(update_fields=['state', 'sent_at', 'attempts', 'updated_at'])
            except Exception as err:
                logger.error('Failed to publish notification', notification_id=notif.id, error=str(err))
                notif.state = Notification.State.FAILED
                notif.last_error = str(err)[:500]
                notif.attempts += 1
                notif.save(update_fields=['state', 'last_error', 'attempts', 'updated_at'])
```

Key points:
- Opens one Kafka connection per batch (not per notification) for efficiency.
- Each notification is published individually so failures are isolated.
- On failure, the notification is marked `FAILED` with the error message.
- No retry logic in this first implementation — failed notifications stay failed.
  Retry can be added later by resetting `FAILED` → `PENDING` on a schedule.

### 6. Replace notifier stubs

- **`notifier/watch.py`**: Remove stub. The `watch_and_notify` function is
  superseded by the Celery Beat `dispatch_notifications` task.
- **`notifier/lsst_return.py`**: Keep stub as-is (LSST return is still TBD).
- **`notifier/impl_http.py`**: Keep stub as-is (future LSST HTTP backend).
- **`run_notifier` management command**: Remove. The notifier now runs via Celery
  Beat, not as a standalone management command.

### 7. Alert status transition to NOTIFIED

After all notifications for an alert are sent, transition the alert to `NOTIFIED`.
This check can run in the dispatch task: after processing a batch, query whether
any alerts now have all their notifications in `sent` state and update accordingly.

```python
# After publishing batch, check for alerts ready to transition
from core.models import Alert
alert_ids = {n.alert_id for n in pending_list if n.state == Notification.State.SENT}
for alert_id in alert_ids:
    has_unsent = Notification.objects.filter(
        alert_id=alert_id
    ).exclude(state=Notification.State.SENT).exists()
    if not has_unsent:
        Alert.objects.filter(pk=alert_id, status=Alert.Status.MATCHED).update(
            status=Alert.Status.NOTIFIED
        )
```

Only transitions when *all* notifications are `SENT` — alerts with any `PENDING`
or `FAILED` notifications remain `MATCHED`.

### 8. Add Hopskotch env vars to Docker and Kubernetes

**`docker/docker-compose.yaml`**: Add Hopskotch env vars to the `celery-worker`
service (workers execute the dispatch task scheduled by Beat):

```yaml
HOPSKOTCH_BROKER_URL: "${HOPSKOTCH_BROKER_URL:-kafka://kafka.scimma.org}"
HOPSKOTCH_TOPIC: "${HOPSKOTCH_TOPIC:-}"
HOPSKOTCH_USERNAME: "${HOPSKOTCH_USERNAME:-}"
HOPSKOTCH_PASSWORD: "${HOPSKOTCH_PASSWORD:-}"
```

The `celery-beat` service only schedules tasks — the worker executes them — so
Beat does not need Hopskotch credentials.

**`docker/.env.example`**: Add Hopskotch section.

**`kubernetes/charts/crossmatch-service/values.yaml`**: Add under a new
`hopskotch` key:

```yaml
hopskotch:
  broker_url: "kafka://kafka.scimma.org"
  topic: ""
  username: ""
  password: ""
```

### 9. Update design document

- Add a new section (e.g., §4.5 or subsection of §4.4) documenting the Hopskotch
  publishing channel: hop-client usage, payload format, Kafka URL, authentication.
- Keep §4.4 as-is for the LSST return channel (still TBD).
- Update the data flow diagram if applicable.

## Acceptance Criteria

- [x] `hop-client` added to `requirements.base.txt`
- [x] Hopskotch env vars added to `settings.py` (BROKER_URL, TOPIC, USERNAME, PASSWORD)
- [x] `crossmatch_batch` creates `Notification` rows after `CatalogMatch` bulk_create
- [x] Notification payload contains: diaObjectId, ra, dec, gaia_source_id, separation_arcsec
- [x] `dispatch_notifications` Celery Beat task polls pending notifications
- [x] Destination handler registry routes by `notification.destination`
- [x] `impl_hopskotch.py` publishes to Hopskotch via `hop-client` `Stream.open()`
- [x] One Kafka connection opened per batch (not per notification)
- [x] Failed notifications marked with `state=FAILED` and `last_error`
- [x] Alerts transition to `NOTIFIED` after all notifications sent
- [x] `notifier/watch.py` stub replaced (or removed)
- [x] Hopskotch env vars added to `docker-compose.yaml` (celery-worker)
- [x] Hopskotch env vars added to `docker/.env.example`
- [x] Hopskotch config added to `kubernetes/values.yaml`
- [x] Design document updated with Hopskotch publishing section
- [x] Notifier logs publish activity (topic, batch size, successes, failures)

## References

- **Origin brainstorm:** `docs/brainstorms/2026-03-12-publish-crossmatch-to-hopskotch-brainstorm.md`
  - Key decisions: hop-client library, Celery Beat periodic task, destination routing,
    split HOPSKOTCH_BROKER_URL + HOPSKOTCH_TOPIC env vars, payload fields
- Notification model: `crossmatch/core/models.py:152-188`
- Crossmatch batch task: `crossmatch/tasks/crossmatch.py:9-78`
- Periodic task pattern: `crossmatch/tasks/schedule.py:7-70`
- Notifier stubs: `crossmatch/notifier/watch.py`, `lsst_return.py`, `impl_http.py`
- Design document §4.4: `scimma_crossmatch_service_design.md`
- hop-client docs: https://hop-client.readthedocs.io/en/stable/
- hop-client source: https://github.com/scimma/hop-client
