---
title: Connect to Remote Dask Scheduler via Environment Variable
type: feat
status: completed
date: 2026-03-16
origin: docs/brainstorms/2026-03-16-dask-scheduler-integration-brainstorm.md
---

# Connect to Remote Dask Scheduler via Environment Variable

## Overview

Add support for connecting to a remote Dask scheduler when deployed on Kubernetes. When the `DASK_SCHEDULER_ADDRESS` env var is set, create a `dask.distributed.Client` at Celery worker startup so that LSDB crossmatch `.compute()` calls execute on the remote Dask cluster. When unset, fall back to Dask's default local synchronous scheduler (current behavior).

(See brainstorm: docs/brainstorms/2026-03-16-dask-scheduler-integration-brainstorm.md)

## Acceptance Criteria

- [x] `DASK_SCHEDULER_ADDRESS` env var read in `project/settings.py` (default: empty string)
- [x] `dask.distributed.Client` created at Celery worker startup via `worker_process_init` signal when `DASK_SCHEDULER_ADDRESS` is set
- [x] No `Client` created when `DASK_SCHEDULER_ADDRESS` is unset or empty (local scheduler fallback)
- [x] Startup logs indicate whether remote or local Dask scheduler is in use
- [x] `DASK_SCHEDULER_ADDRESS` added to Helm chart env vars (`templates/_helpers.yaml`)
- [x] `DASK_SCHEDULER_ADDRESS` added to celery-worker env in `docker/docker-compose.yaml` (commented out, with note)
- [x] Design document updated to reflect Dask scheduler integration

## Changes

### 1. `crossmatch/project/settings.py`

Add the setting:

```python
# Dask distributed scheduler (optional)
# When set, Celery workers connect to a remote Dask scheduler.
# When empty, Dask uses its default local synchronous scheduler.
# In K8s, set from HOPDEVEL_DASK_SCHEDULER_SERVICE_HOST and
# HOPDEVEL_DASK_SCHEDULER_SERVICE_PORT_TCP_COMM.
DASK_SCHEDULER_ADDRESS = os.getenv('DASK_SCHEDULER_ADDRESS', '')
```

### 2. `crossmatch/core/dask.py` (new file)

Create a Dask client initialization module following the pattern in `core/k8s.py`:

```python
from celery.signals import worker_process_init
from django.conf import settings
from core.log import get_logger

logger = get_logger(__name__)

@worker_process_init.connect
def connect_dask_scheduler(**kwargs):
    """Connect to remote Dask scheduler if DASK_SCHEDULER_ADDRESS is set."""
    address = settings.DASK_SCHEDULER_ADDRESS
    if not address:
        logger.info('No DASK_SCHEDULER_ADDRESS set, using local Dask scheduler')
        return

    from dask.distributed import Client
    logger.info('Connecting to remote Dask scheduler', address=address)
    try:
        Client(address)  # Sets this as the default scheduler for the process
        logger.info('Connected to remote Dask scheduler', address=address)
    except Exception:
        logger.exception('Failed to connect to Dask scheduler, falling back to local',
                         address=address)
```

Notes:
- `dask.distributed.Client()` automatically registers itself as the default Dask scheduler for the process. No need to pass it to `.compute()` calls — LSDB will use it automatically.
- If the connection fails, the worker starts anyway using the local scheduler. This prevents crash loops when the Dask cluster is temporarily unavailable.

### 3. `crossmatch/project/celery.py`

Import the new module so the signal handler is registered:

```python
# Connect to remote Dask scheduler if configured
from core import dask  # noqa: F401 — registers worker_process_init signal
```

This import should be unconditional (unlike the K8s liveness probe which is conditional on `KUBERNETES_SERVICE_HOST`). The signal handler itself checks `DASK_SCHEDULER_ADDRESS` and no-ops when empty.

### 4. `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`

Add to the `celery.env` define block:

```yaml
- name: DASK_SCHEDULER_ADDRESS
  value: {{ .Values.celery.dask_scheduler_address | default "" | quote }}
```

### 5. `kubernetes/charts/crossmatch-service/values.yaml`

Add under the `celery:` section:

```yaml
  # Remote Dask scheduler address (e.g., tcp://10.0.0.5:8786)
  # Set from K8s service discovery: tcp://${HOPDEVEL_DASK_SCHEDULER_SERVICE_HOST}:${HOPDEVEL_DASK_SCHEDULER_SERVICE_PORT_TCP_COMM}
  dask_scheduler_address: ""
```

### 6. `docker/docker-compose.yaml`

Add to the celery-worker environment (commented out since local dev doesn't use a remote scheduler):

```yaml
      # Remote Dask scheduler (not used in local dev)
      # DASK_SCHEDULER_ADDRESS: "${DASK_SCHEDULER_ADDRESS:-}"
```

### 7. `scimma_crossmatch_service_design.md`

Update §2.1 C (Crossmatch Workers) to note the optional remote Dask scheduler connection, and §9.1.3 to add `DASK_SCHEDULER_ADDRESS` env var.

## Open Questions (from brainstorm)

1. **Version alignment** — The Dask cluster currently runs Python 3.10. The plan is to upgrade the cluster to Python 3.12. The Client wiring can be implemented now, but end-to-end testing requires version alignment. Tracked separately.

2. **Client reconnection** — If the Dask scheduler restarts, does `distributed.Client` reconnect automatically? If not, the Celery worker would need to be restarted. To be verified during testing.

3. **Celery concurrency vs Dask** — Multiple Celery tasks can submit to Dask concurrently. Whether concurrency should be tuned is deferred to testing.

## Sources

- **Origin brainstorm:** [docs/brainstorms/2026-03-16-dask-scheduler-integration-brainstorm.md](docs/brainstorms/2026-03-16-dask-scheduler-integration-brainstorm.md) — Key decisions: single `DASK_SCHEDULER_ADDRESS` env var, Client at worker startup, local fallback when unset
- **Colleague's findings:** [dask.md](dask.md) — Dask cluster setup and K8s env vars
- **Existing signal pattern:** `crossmatch/core/k8s.py` — Celery signal handler pattern
- **Celery app:** `crossmatch/project/celery.py` — Where to register the signal import
- **Helm env vars:** `kubernetes/charts/crossmatch-service/templates/_helpers.yaml:31-36`
