# Brainstorm: Add Local Dask Scheduler to Docker Compose

**Date:** 2026-03-19
**Status:** Draft

## What We're Building

Add a local Dask scheduler service to docker-compose using official Dask container images, with LSDB and dependencies installed via `EXTRA_PIP_PACKAGES`. This enables local testing of the remote Dask scheduler code path without needing a Kubernetes cluster.

## Why This Approach

The crossmatch-service has code to connect to a remote Dask scheduler (`core/dask.py`, `DASK_SCHEDULER_ADDRESS`), but local development has only been tested with Dask's local in-process scheduler. Adding a local Dask scheduler to docker-compose enables testing the distributed code path. For Kubernetes deployments, the Dask cluster is managed by a separate project since it's shared infrastructure.

## Key Decisions

### 1. Use official Dask image `ghcr.io/dask/dask:2026.1.2-py3.12`

The official image matches our Python 3.12 and dask 2026.1.2 versions. The same image runs both the scheduler (`dask-scheduler` command) and workers (`dask-worker` command). No custom Dockerfile needed. The profile includes two services: a `dask-scheduler` and a `dask-worker`. The scheduler coordinates task distribution; the worker executes the actual crossmatch computations.

### 2. Install LSDB via `EXTRA_PIP_PACKAGES`

The Dask image supports `EXTRA_PIP_PACKAGES` to install additional packages at startup via pip. Set it to `lsdb==0.8.1 numpy==2.4.2 pandas==2.3.3` on the **worker** (which deserializes and runs the task code) to match the crossmatch-service's pinned versions. The scheduler does not need these packages since it only coordinates — it doesn't execute tasks. This adds startup time (~30-60 seconds) to the worker but avoids maintaining a custom image for local dev.

### 3. Behind a `dask-scheduler` profile

The Dask service only starts when explicitly requested with `docker compose --profile dask-scheduler up`. When the profile is not active, crossmatches use Dask's local in-process scheduler (current default behavior). This follows the pattern established by the `local-kafka` profile.

### 4. Developer toggles `DASK_SCHEDULER_ADDRESS` in `.env`

When using the dask-scheduler profile, set `DASK_SCHEDULER_ADDRESS=tcp://dask-scheduler:8786` in `.env`. When not using it, leave it empty. Both options documented with comments in `.env.example`.

### 5. Kubernetes deployments manage Dask separately

The docker-compose Dask service is for local dev only. In Kubernetes, the Dask cluster is managed by a separate project since it serves multiple consumers. The Helm chart's `celery.dask_scheduler_address` value points to the K8s service DNS name.

## Open Questions

None — all decisions made during brainstorming.
