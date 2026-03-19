---
title: Add Local Dask Scheduler to Docker Compose
type: feat
status: completed
date: 2026-03-19
origin: docs/brainstorms/2026-03-19-local-dask-scheduler-docker-compose-brainstorm.md
---

# Add Local Dask Scheduler to Docker Compose

## Overview

Add `dask-scheduler` and `dask-worker` services to docker-compose behind a `dask-scheduler` profile, using the official Dask image (`ghcr.io/dask/dask:2026.1.2-py3.12`). The worker installs LSDB via `EXTRA_PIP_PACKAGES` at startup for task deserialization compatibility.

(See brainstorm: docs/brainstorms/2026-03-19-local-dask-scheduler-docker-compose-brainstorm.md)

## Acceptance Criteria

- [x] `dask-scheduler` service added to docker-compose behind `dask-scheduler` profile
- [x] `dask-worker` service added to docker-compose behind `dask-scheduler` profile, with `EXTRA_PIP_PACKAGES` installing `lsdb==0.8.1 numpy==2.4.2 pandas==2.3.3`
- [x] Worker depends on scheduler being healthy
- [x] Both services on the `internal` network
- [x] `.env.example` documents `DASK_SCHEDULER_ADDRESS` with instructions for both modes
- [ ] `docker compose --profile dask-scheduler up` starts scheduler + worker successfully
- [ ] Celery worker connects to local Dask scheduler when `DASK_SCHEDULER_ADDRESS=tcp://dask-scheduler:8786`

## Changes

### 1. `docker/docker-compose.yaml`

Add two services after the `local-kafka-init` service, following the `local-kafka` profile pattern:

```yaml
  dask-scheduler:
    image: ghcr.io/dask/dask:2026.1.2-py3.12
    command: ["dask-scheduler"]
    hostname: dask-scheduler
    profiles:
      - dask-scheduler
    networks:
      internal:
        aliases:
          - dask-scheduler
    ports:
      - 127.0.0.1:8786:8786
      - 127.0.0.1:8787:8787
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8787/health')"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 10s

  dask-worker:
    image: ghcr.io/dask/dask:2026.1.2-py3.12
    command: ["dask-worker", "dask-scheduler:8786"]
    profiles:
      - dask-scheduler
    depends_on:
      dask-scheduler:
        condition: service_healthy
    networks:
      - internal
    environment:
      EXTRA_PIP_PACKAGES: "lsdb==0.8.1 numpy==2.4.2 pandas==2.3.3"
```

Port 8786 is the scheduler communication port. Port 8787 is the Dask dashboard (web UI for monitoring).

### 2. `docker/.env.example`

Update the Dask scheduler section with both modes documented:

```
# ----- Dask scheduler -----
# Leave empty for local Dask (default). Set to connect to a remote scheduler.
# When using the dask-scheduler profile (docker compose --profile dask-scheduler up):
#DASK_SCHEDULER_ADDRESS=tcp://dask-scheduler:8786
DASK_SCHEDULER_ADDRESS=
```

## Usage

```bash
# Start with local Dask scheduler:
docker compose --profile dask-scheduler up -d

# Set in .env:
DASK_SCHEDULER_ADDRESS=tcp://dask-scheduler:8786

# Dask dashboard available at http://localhost:8787

# Start without (default local in-process Dask):
docker compose up -d
# DASK_SCHEDULER_ADDRESS= (empty)
```

## Sources

- **Origin brainstorm:** [docs/brainstorms/2026-03-19-local-dask-scheduler-docker-compose-brainstorm.md](docs/brainstorms/2026-03-19-local-dask-scheduler-docker-compose-brainstorm.md) — Key decisions: official Dask image, EXTRA_PIP_PACKAGES for lsdb, behind profile, K8s manages Dask separately
- **Profile pattern:** `docker/docker-compose.yaml` local-kafka profile (lines 248-288)
- **Dask Docker docs:** https://docs.dask.org/en/latest/deploying-docker.html
