---
title: "Local Kafka server for Hopskotch testing"
type: feat
status: completed
date: 2026-03-13
origin: docs/brainstorms/2026-03-13-local-kafka-for-hopskotch-testing-brainstorm.md
---

# Local Kafka Server for Hopskotch Testing

## Overview

Add a local Kafka server (`scimma/server:latest`) to Docker Compose and
Kubernetes for testing the Hopskotch publisher without production credentials.
Disabled by default — enabled via a Docker Compose profile (`local-kafka`) or
Kubernetes replicas override. The publisher code is updated to pass `auth=False`
when credentials are empty.

## Proposed Solution

### 1. Update `impl_hopskotch.py` to support no-auth mode

If `HOPSKOTCH_USERNAME` is empty, pass `auth=False` to `Stream()` instead of
creating an `Auth` instance (see brainstorm decision #4).

```python
# notifier/impl_hopskotch.py
from hop import Stream
from hop.auth import Auth

if settings.HOPSKOTCH_USERNAME:
    auth = Auth(user=settings.HOPSKOTCH_USERNAME, password=settings.HOPSKOTCH_PASSWORD)
else:
    auth = False

stream = Stream(auth=auth)
```

File: `crossmatch/notifier/impl_hopskotch.py:20-22`

### 2. Add `local-kafka` service to `docker/docker-compose.yaml`

Add a new service under the `local-kafka` Compose profile:

```yaml
  local-kafka:
    image: scimma/server:latest
    command: ["--noSecurity"]
    hostname: local-kafka
    profiles:
      - local-kafka
    networks:
      internal:
        aliases:
          - local-kafka
    ports:
      - 127.0.0.1:9092:9092
    healthcheck:
      test: ["CMD-SHELL", "nc -z localhost 9092 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 30s
```

Key points:
- `profiles: [local-kafka]` — only starts with `--profile local-kafka`
- `hostname: local-kafka` — Kafka advertised listener must match the DNS name
  other containers use. If this doesn't work (the wiki says `--hostname localhost`),
  fall back to `hostname: localhost` with network alias `local-kafka` and test.
- Port 9092 exposed to host for hop CLI verification
- Health check with 30s start period (scimma/server takes ~20s to start)
- Topics are created implicitly on first publish — no init step needed

### 3. Update `docker/.env.example` with local Kafka instructions

Add a commented section showing how to configure for local Kafka:

```
# ----- Hopskotch publisher -----
# For production Hopskotch:
HOPSKOTCH_BROKER_URL=kafka://kafka.scimma.org
HOPSKOTCH_TOPIC=
HOPSKOTCH_USERNAME=
HOPSKOTCH_PASSWORD=
# For local Kafka testing (docker compose --profile local-kafka up),
# uncomment these and comment out the production values above:
#HOPSKOTCH_BROKER_URL=kafka://local-kafka:9092
#HOPSKOTCH_TOPIC=crossmatch-test
```

### 4. Add local-kafka to Kubernetes `values.yaml`

```yaml
local_kafka:
  enabled: false
  image: scimma/server:latest
  replicas: 0
```

When enabled, the user overrides `hopskotch.*` values to point at the local
Kafka service, same as the Docker Compose `.env` approach.

### 5. Update design document

Add a brief note to §4.5 (Hopskotch publishing) mentioning the local Kafka
testing option and how to enable it.

## Acceptance Criteria

- [x] `impl_hopskotch.py` passes `auth=False` when `HOPSKOTCH_USERNAME` is empty
- [x] `local-kafka` service added to `docker-compose.yaml` with profile `local-kafka`
- [x] Service uses `scimma/server:latest` with `--noSecurity`
- [x] Port 9092 exposed to host for hop CLI access
- [x] Health check with appropriate start period (~30s)
- [x] `docker compose up` (without `--profile`) does NOT start local-kafka
- [x] `docker compose --profile local-kafka up` starts local-kafka
- [x] `.env.example` updated with commented local Kafka configuration
- [x] `values.yaml` updated with `local_kafka` section (replicas: 0)
- [x] Design document updated with local Kafka testing note

## References

- **Origin brainstorm:** `docs/brainstorms/2026-03-13-local-kafka-for-hopskotch-testing-brainstorm.md`
  - Key decisions: scimma/server image, Compose profile, user updates .env,
    empty credentials = auth=False, K8s replicas 0
- Hopskotch publisher: `crossmatch/notifier/impl_hopskotch.py`
- Docker Compose: `docker/docker-compose.yaml`
- Kubernetes values: `kubernetes/charts/crossmatch-service/values.yaml`
- scimma/server image: https://hub.docker.com/r/scimma/server
- hop-client wiki (local Kafka): https://github.com/scimma/hop-client/wiki
