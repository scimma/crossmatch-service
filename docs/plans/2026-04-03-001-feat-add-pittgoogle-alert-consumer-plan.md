---
title: Add Pitt-Google Alert Consumer
type: feat
status: completed
date: 2026-04-03
origin: docs/brainstorms/2026-04-03-add-pittgoogle-alert-consumer-requirements.md
---

# Add Pitt-Google Alert Consumer

## Overview

Add a third alert consumer for the Pitt-Google broker, which serves LSST alerts via Google Cloud Pub/Sub. This follows the established ANTARES/Lasair consumer pattern: a broker-specific consumer module, normalization function, management command, entrypoint script, and infrastructure wiring (docker-compose, Helm chart).

The key difference from existing consumers is that Pitt-Google uses a callback-based `pittgoogle.pubsub.Consumer.stream()` instead of a polling loop. The consumer architecture adapts this by wrapping `stream()` in the same outer reconnection loop with exponential backoff.

(See origin: docs/brainstorms/2026-04-03-add-pittgoogle-alert-consumer-requirements.md)

## Acceptance Criteria

- [x] `pittgoogle-client` added to `requirements.base.txt`
- [x] `crossmatch/brokers/pittgoogle/__init__.py` created (empty)
- [x] `crossmatch/brokers/pittgoogle/consumer.py` created with `consume_alerts()` function
- [x] `normalize_pittgoogle()` added to `crossmatch/brokers/normalize.py`
- [x] Pitt-Google settings section added to `crossmatch/project/settings.py`
- [x] `run_pittgoogle_ingest` management command created
- [x] `run_pittgoogle_ingest.sh` entrypoint script created
- [x] `pittgoogle-consumer` service added to `docker/docker-compose.yaml`
- [x] Pitt-Google section added to `docker/.env.example`
- [x] `pittgoogle_consumer` section added to Helm `values.yaml`
- [x] `pittgoogle.env` helper added to Helm `_helpers.yaml`
- [x] Pitt-Google StatefulSet added to Helm `statefulset.yaml` with GCP Secret volume mount
- [x] Pitt-Google overrides added to `kubernetes/dev-overrides.yaml.example`
- [x] Design document updated with Pitt-Google broker section

## Changes

### 1. `crossmatch/requirements.base.txt`

Add after `lasair`:

```
pittgoogle-client
```

**Note:** Verify compatibility with Python 3.12, numpy==2.4.2, pandas==2.3.3 during implementation. The `pittgoogle-client` package depends on `google-cloud-pubsub` which should have no conflicts with the existing pinned dependencies.

### 2. `crossmatch/brokers/pittgoogle/__init__.py`

Create empty file (matches `antares/__init__.py` and `lasair/__init__.py` pattern).

### 3. `crossmatch/brokers/pittgoogle/consumer.py`

Create following the established consumer pattern. Key adaptation: `Consumer.stream()` is callback-based and blocks indefinitely, so the outer `while True` reconnection loop wraps the entire stream lifecycle rather than individual polls.

```python
"""Pitt-Google Pub/Sub alert consumer.

Pitt-Google broker: Google Cloud Pub/Sub
Topic: lsst-alerts (project: pitt-alert-broker)
Auth: GCP service account (GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS)
Filter: attribute filter 'attributes:diaObject_diaObjectId' applied server-side
"""
import time

import pittgoogle
from django.conf import settings
from brokers import ingest_alert
from brokers.normalize import normalize_pittgoogle
from core.log import get_logger

logger = get_logger(__name__)

BROKER_NAME = 'pittgoogle'
_BACKOFF_INITIAL = 1    # seconds
_BACKOFF_MAX = 60       # seconds


def _msg_callback(alert):
    """Process a single alert from the Pub/Sub stream.

    Ack on success or permanent failure (malformed data).
    Nack only on transient failure (database error) so Pub/Sub redelivers.
    """
    try:
        canonical = normalize_pittgoogle(alert)
    except Exception as err:
        # Permanent failure: malformed alert data will never normalize.
        # Ack to prevent infinite redelivery (matches ANTARES inner try/except
        # pattern where normalization errors are caught and the loop continues).
        logger.error(
            'Failed to normalize Pitt-Google alert, acking to discard',
            error=str(err),
        )
        return pittgoogle.pubsub.Response(ack=True, result=None)

    try:
        ingest_alert(canonical, broker=BROKER_NAME)
    except Exception as err:
        # Transient failure: database error, connection issue, etc.
        # Nack so Pub/Sub redelivers after the ack deadline.
        logger.error(
            'Failed to ingest Pitt-Google alert, nacking for redelivery',
            error=str(err),
            diaObjectId=canonical.get('lsst_diaObject_diaObjectId'),
        )
        return pittgoogle.pubsub.Response(ack=False, result=None)

    return pittgoogle.pubsub.Response(ack=True, result=None)


def consume_alerts():
    """Subscribe to the Pitt-Google lsst-alerts topic and ingest alerts.

    Uses pittgoogle.pubsub.Consumer.stream() which blocks indefinitely,
    dispatching alerts to _msg_callback in a thread pool. On stream failure
    (network error, credential issue), reconnects with exponential backoff
    matching the ANTARES/Lasair consumer pattern.
    """
    topic = pittgoogle.Topic(
        name=settings.PITTGOOGLE_TOPIC,
        projectid=settings.PITTGOOGLE_PUBLISHER_PROJECT,
    )
    subscription = pittgoogle.Subscription(
        name=settings.PITTGOOGLE_SUBSCRIPTION,
        topic=topic,
        schema_name='lsst',
    )

    backoff = _BACKOFF_INITIAL
    while True:
        try:
            logger.info(
                'Creating/verifying Pitt-Google Pub/Sub subscription...',
                subscription=settings.PITTGOOGLE_SUBSCRIPTION,
                topic=settings.PITTGOOGLE_TOPIC,
                publisher_project=settings.PITTGOOGLE_PUBLISHER_PROJECT,
            )
            subscription.touch(attribute_filter='attributes:diaObject_diaObjectId')

            consumer = pittgoogle.pubsub.Consumer(
                subscription=subscription,
                msg_callback=_msg_callback,
            )
            logger.info('Listening for Pitt-Google alerts...')
            consumer.stream()  # blocks indefinitely
        except Exception as err:
            logger.error(f'Pitt-Google streaming error: {err}')
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
```

**Design decisions:**
- **Two-level error handling in callback** (see origin: R2, success criteria): normalization errors ack (permanent — redelivery won't fix malformed data), ingest errors nack (transient — database may recover). This matches the ANTARES pattern where inner try/except catches per-alert errors while outer try/except handles connection errors.
- **Reconnection loop wraps `stream()`**: If `stream()` exits (network partition, credential revocation, library error), the outer `while True` with exponential backoff reconnects. This is safer than relying solely on container restart, which has a slower feedback loop.
- **Thread safety**: `Consumer.stream()` dispatches callbacks via `ThreadPoolExecutor`. The `ingest_alert()` function uses `get_or_create` with `unique=True` constraints, which handles concurrent calls via `IntegrityError` at the database level. No additional locking needed.

### 4. `crossmatch/brokers/normalize.py`

Add `normalize_pittgoogle()` after `normalize_lasair()`:

```python
def normalize_pittgoogle(alert) -> dict:
    """Normalize a Pitt-Google alert to the internal canonical format.

    The pittgoogle.Alert object exposes LSST fields directly:
    .objectid (diaObjectId), .sourceid (diaSourceId), .ra, .dec, .dict (full payload).

    event_time is derived from the LSST alert's MJD-TAI timestamp, converted
    to a datetime using the same _MJD_EPOCH pattern as normalize_lasair().
    """
    mjd_tai = alert.dict.get('midpointMjdTai') or alert.dict.get('midPointTai')
    if mjd_tai is not None:
        event_time = _MJD_EPOCH + timedelta(days=mjd_tai)
    else:
        event_time = datetime.now(tz=timezone.utc)

    return {
        'lsst_diaObject_diaObjectId': alert.objectid,
        'ra_deg': alert.ra,
        'dec_deg': alert.dec,
        'lsst_diaSource_diaSourceId': alert.sourceid,
        'event_time': event_time,
        'payload': alert.dict,
    }
```

**Note on `event_time`:** The exact field name for the LSST alert timestamp needs verification during implementation (see origin: Outstanding Questions). The LSST alert schema likely uses `midpointMjdTai` for the observation midpoint. The code converts MJD-TAI to a `datetime` using the same `_MJD_EPOCH + timedelta(days=...)` pattern as `normalize_lasair()`, falling back to `datetime.now(tz=timezone.utc)` if neither candidate field is present. The field name will be confirmed by inspecting a real alert during implementation.

**Note on `alert.dict` serializability:** The `alert.dict` property should return JSON-compatible data since the `lsst-alerts` topic uses Avro serialization decoded by the client. If any non-serializable types are encountered (bytes from cutout stamps), use `alert.drop_cutouts()` before accessing `.dict`, or handle in a try/except.

### 5. `crossmatch/project/settings.py`

Add after the ANTARES section (line 184):

```python
######################################################################
# Pitt-Google Pub/Sub consumer
#
PITTGOOGLE_TOPIC = os.environ.get('PITTGOOGLE_TOPIC', 'lsst-alerts')
PITTGOOGLE_SUBSCRIPTION = os.environ.get('PITTGOOGLE_SUBSCRIPTION', 'scimma-crossmatch-lsst-alerts')
PITTGOOGLE_PUBLISHER_PROJECT = os.environ.get('PITTGOOGLE_PUBLISHER_PROJECT', 'pitt-alert-broker')
# GCP auth is handled by standard env vars:
#   GOOGLE_CLOUD_PROJECT — the subscriber's GCP project (where the subscription lives)
#   GOOGLE_APPLICATION_CREDENTIALS — path to service account JSON key file
```

**Env var summary:**

| Env Var | Purpose | Default | Where Set |
|---------|---------|---------|-----------|
| `PITTGOOGLE_TOPIC` | Pitt-Google topic name | `lsst-alerts` | settings, compose, Helm |
| `PITTGOOGLE_SUBSCRIPTION` | Pub/Sub subscription name | `scimma-crossmatch-lsst-alerts` | settings, compose, Helm |
| `PITTGOOGLE_PUBLISHER_PROJECT` | Pitt-Google's GCP project ID | `pitt-alert-broker` | settings, compose, Helm |
| `GOOGLE_CLOUD_PROJECT` | Subscriber's GCP project ID | (none) | compose, Helm Secret |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to SA key file | (none) | compose, Helm (fixed path) |

### 6. `crossmatch/project/management/commands/run_pittgoogle_ingest.py`

```python
from django.core.management.base import BaseCommand
from brokers.pittgoogle.consumer import consume_alerts


class Command(BaseCommand):
    help = "Run Pitt-Google alert ingest"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting Pitt-Google alert ingest...'))
        consume_alerts()
```

### 7. `crossmatch/entrypoints/run_pittgoogle_ingest.sh`

Follow the Lasair entrypoint pattern (simpler than ANTARES — no MAKE_MIGRATIONS gate):

```bash
#!/bin/bash

set -euo pipefail

## Initialize Django database and static files
##
cd "${APP_ROOT_DIR:-/opt}/crossmatch"
bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT} --timeout=0
bash entrypoints/wait-for-it.sh ${VALKEY_SERVICE:-redis}:${VALKEY_PORT:-6379} --timeout=0

echo "Running initialization script..."
bash entrypoints/django_init.sh
echo "Django database initialization complete."

# Start Pitt-Google alert ingest
cd "${APP_ROOT_DIR:-/opt}/crossmatch"

python manage.py run_pittgoogle_ingest
```

### 8. `docker/docker-compose.yaml`

Add `pittgoogle-consumer` service after `lasair-consumer` (after line 87). Follows the Lasair service pattern with additional GCP credential handling:

```yaml
  pittgoogle-consumer:
    image: 585193511743.dkr.ecr.us-west-2.amazonaws.com/scimma/crossmatch-service:dev
    platform: linux/x86_64
    networks:
      - internal
    build:
      context: ../crossmatch
      dockerfile: ../docker/Dockerfile
      args:
        UID: "${USERID:-1000}"
    command: bash entrypoints/run_pittgoogle_ingest.sh
    environment:
      DJANGO_DEBUG: "${DJANGO_DEBUG:-true}"
      LOG_LEVEL: ${LOG_LEVEL:-DEBUG}
      DEV_MODE: "${DEV_MODE:-true}"
      # Django base configuration
      DJANGO_SUPERUSER_PASSWORD: "${DJANGO_SUPERUSER_PASSWORD:-password}"
      DJANGO_SUPERUSER_USERNAME: "${DJANGO_SUPERUSER_USERNAME:-admin}"
      DJANGO_SUPERUSER_EMAIL: "${DJANGO_SUPERUSER_EMAIL:-test@example.com}"
      # Django database configuration
      DATABASE_HOST: "${DATABASE_HOST:-django-db}"
      DATABASE_PORT: "${DATABASE_PORT:-5432}"
      DATABASE_DB: "${DATABASE_DB:-scimma_crossmatch_service}"
      DATABASE_USER: "${DATABASE_USER:-crossmatch_service_admin}"
      DATABASE_PASSWORD: "${DATABASE_PASSWORD:-password}"
      VALKEY_SERVICE: "${VALKEY_SERVICE:-redis}"
      VALKEY_PORT: "${VALKEY_PORT:-6379}"
      APP_ROOT_DIR: "${APP_ROOT_DIR:-/opt}"
      # Pitt-Google broker configuration
      PITTGOOGLE_TOPIC: "${PITTGOOGLE_TOPIC:-lsst-alerts}"
      PITTGOOGLE_SUBSCRIPTION: "${PITTGOOGLE_SUBSCRIPTION:-scimma-crossmatch-lsst-alerts}"
      PITTGOOGLE_PUBLISHER_PROJECT: "${PITTGOOGLE_PUBLISHER_PROJECT:-pitt-alert-broker}"
      # GCP auth
      GOOGLE_CLOUD_PROJECT: "${GOOGLE_CLOUD_PROJECT:-}"
      GOOGLE_APPLICATION_CREDENTIALS: /var/run/secrets/gcp/key.json
    volumes:
      - ../crossmatch:/opt/crossmatch
      - ${GCP_KEY_FILE:-./secrets/gcp-pittgoogle-key.json}:/var/run/secrets/gcp/key.json:ro
```

**GCP key file mount:** The service account JSON key is bind-mounted from the host. Developers place their key file at `docker/secrets/gcp-pittgoogle-key.json` (or override via `GCP_KEY_FILE` env var). The `secrets/` directory should be gitignored.

### 9. `docker/.env.example`

Add after the Lasair section (after line 44):

```
# ----- Pitt-Google broker -----
# Requires a GCP service account key file.
# Place at docker/secrets/gcp-pittgoogle-key.json (or set GCP_KEY_FILE path).
GOOGLE_CLOUD_PROJECT=
GCP_KEY_FILE=./secrets/gcp-pittgoogle-key.json
PITTGOOGLE_TOPIC=lsst-alerts
PITTGOOGLE_SUBSCRIPTION=scimma-crossmatch-lsst-alerts
PITTGOOGLE_PUBLISHER_PROJECT=pitt-alert-broker
```

### 10. `kubernetes/charts/crossmatch-service/values.yaml`

Add `pittgoogle_consumer` section after `lasair_consumer` (after line 46):

```yaml
pittgoogle_consumer:
  enabled: true
  replicaCount: 1
  service:
    host: "pittgoogle-consumer"
  topic: "lsst-alerts"
  subscription: "scimma-crossmatch-lsst-alerts"
  publisher_project: "pitt-alert-broker"
  resources:
    requests:
      cpu: '500m'
      memory: 100Mi
    limits:
      cpu: '2'
      memory: 2Gi
```

GCP credentials (`GOOGLE_CLOUD_PROJECT` and the service account key file) are provided via a K8s Secret named `gcp-pittgoogle`, not via Helm values (see origin: R4 — credentials never in Helm values).

### 11. `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`

Add `pittgoogle.env` helper after `antares.env` (after line 87):

```yaml
{{- define "pittgoogle.env" -}}
- name: PITTGOOGLE_TOPIC
  value: {{ .Values.pittgoogle_consumer.topic | quote }}
- name: PITTGOOGLE_SUBSCRIPTION
  value: {{ .Values.pittgoogle_consumer.subscription | quote }}
- name: PITTGOOGLE_PUBLISHER_PROJECT
  value: {{ .Values.pittgoogle_consumer.publisher_project | quote }}
- name: GOOGLE_CLOUD_PROJECT
  valueFrom:
    secretKeyRef:
      key: GOOGLE_CLOUD_PROJECT
      name: gcp-pittgoogle
- name: GOOGLE_APPLICATION_CREDENTIALS
  value: "/var/run/secrets/gcp/key.json"
{{- end }}
```

### 12. `kubernetes/charts/crossmatch-service/templates/statefulset.yaml`

Add Pitt-Google StatefulSet at the end of the file (after line 189):

```yaml
{{- if .Values.pittgoogle_consumer.enabled }}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ .Values.pittgoogle_consumer.service.host | quote }}
  {{- with .Values.common.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  replicas: {{ .Values.pittgoogle_consumer.replicaCount }}
  serviceName: {{ .Values.pittgoogle_consumer.service.host | quote }}
  selector:
    matchLabels:
      app: {{ .Values.pittgoogle_consumer.service.host | quote }}
  template:
    metadata:
      labels:
        app: {{ .Values.pittgoogle_consumer.service.host | quote }}
        app.kubernetes.io/component: pittgoogle-consumer
    spec:
      containers:
      - name: pittgoogle-consumer
        image: {{ .Values.common.image.repo }}:{{ .Values.common.image.tag }}
        imagePullPolicy: {{ .Values.common.image.imagePullPolicy }}
        command:
        - /bin/bash
        - -c
        - bash entrypoints/run_pittgoogle_ingest.sh
        env:
          {{- include "common.env" . | nindent 10 }}
          {{- include "django.env" . | nindent 10 }}
          {{- include "db.env" . | nindent 10 }}
          {{- include "valkey.env" . | nindent 10 }}
          {{- include "celery.env" . | nindent 10 }}
          {{- include "pittgoogle.env" . | nindent 10 }}
        volumeMounts:
        - name: gcp-key
          mountPath: /var/run/secrets/gcp
          readOnly: true
        {{- with .Values.pittgoogle_consumer.resources }}
        resources:
          {{- toYaml . | nindent 10 }}
        {{- end }}
      volumes:
      - name: gcp-key
        secret:
          secretName: gcp-pittgoogle
          items:
          - key: key.json
            path: key.json
{{- end }}
```

**Secret volume mount:** The GCP service account key file is mounted from a K8s Secret named `gcp-pittgoogle` at `/var/run/secrets/gcp/key.json`. This keeps the credential out of env vars and Helm values (see origin: R4).

### 13. `kubernetes/dev-overrides.yaml.example`

Add Pitt-Google section and secret creation command. Add to the header comment block:

```
#   kubectl create secret generic gcp-pittgoogle --from-file=key.json=/path/to/sa-key.json --from-literal=GOOGLE_CLOUD_PROJECT='my-gcp-project-id'
```

Add values section after `antares_consumer`:

```yaml
pittgoogle_consumer:
  topic: "lsst-alerts"
  subscription: "scimma-crossmatch-dev-lsst-alerts"
```

### 14. `scimma_crossmatch_service_design.md`

Add a new section (§4.4 or similar) for the Pitt-Google broker interface, following the pattern of §4.1 (ANTARES) and §4.3 (Lasair). Include:

- Broker name and transport (Google Cloud Pub/Sub)
- Topic: `lsst-alerts` in project `pitt-alert-broker`
- Authentication: GCP service account
- Consumer library: `pittgoogle-client`
- Consumer pattern: callback-based `Consumer.stream()`
- Attribute filter: `attributes:diaObject_diaObjectId`
- Environment variables table

Also update the broker enumeration in §2.1 to list Pitt-Google as an active broker alongside ANTARES and Lasair.

### 15. `.gitignore`

Add `docker/secrets/` if not already present, to prevent accidental commit of GCP key files.

## Technical Considerations

### Callback vs. Polling Architecture

The existing ANTARES and Lasair consumers use polling loops (`client.iter()` / `consumer.poll()`). Pitt-Google uses a callback-based `Consumer.stream()` that dispatches alerts to a `ThreadPoolExecutor`. This difference is handled by:
- Moving alert processing into a standalone `_msg_callback` function
- Wrapping `stream()` in the same outer reconnection loop used by ANTARES/Lasair
- Using `Response(ack=True/False)` instead of implicit Kafka offset advancement

### Error Handling Strategy

Two categories of errors require different responses (see origin: success criteria):

| Error Type | Example | Action | Rationale |
|-----------|---------|--------|-----------|
| Permanent | `KeyError` in normalization, malformed data | `Response(ack=True)` — discard | Redelivery will not fix the data |
| Transient | Database connection error, operational error | `Response(ack=False)` — redeliver | Database may recover |

This matches the ANTARES pattern where inner `try/except` catches per-alert errors (log and continue = implicit ack) while outer `try/except` handles connection errors (backoff and reconnect).

### Thread Safety

`Consumer.stream()` uses a `ThreadPoolExecutor` for concurrent callback dispatch. The `ingest_alert()` function is thread-safe because:
- `Alert.objects.get_or_create()` is protected by the `unique=True` constraint on `lsst_diaObject_diaObjectId`
- `AlertDelivery.objects.get_or_create()` is protected by the `unique_together` constraint on `(alert, broker)`
- Django's ORM handles `IntegrityError` from concurrent `get_or_create` calls gracefully

### Subscription Lifecycle

- `subscription.touch()` creates the subscription if it doesn't exist, no-ops if it does
- The attribute filter `attributes:diaObject_diaObjectId` is immutable once set
- If a subscription exists with a different filter, `touch()` will not update it — the consumer will log a startup message but no automatic remediation (manual deletion and recreation required)
- Each environment (dev, prod) should use a distinct subscription name to prevent message splitting

## Sources

- **Origin document:** [docs/brainstorms/2026-04-03-add-pittgoogle-alert-consumer-requirements.md](docs/brainstorms/2026-04-03-add-pittgoogle-alert-consumer-requirements.md) — Key decisions: lsst-alerts topic, callback-based Consumer.stream(), server-side attribute filter, GCP service account auth with mounted key file
- **ANTARES consumer pattern:** `crossmatch/brokers/antares/consumer.py` — reconnection loop, inner/outer error handling
- **Lasair consumer pattern:** `crossmatch/brokers/lasair/consumer.py` — polling loop, backoff reset on healthy state
- **Pitt-Google client docs:** https://mwvgroup.github.io/pittgoogle-client/
- **Pub/Sub demo:** https://github.com/mwvgroup/pittgoogle-user-demos/blob/main/pubsub/README.md
