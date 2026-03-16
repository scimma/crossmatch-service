# Brainstorm: Dask Scheduler Integration via Environment Variable

**Date:** 2026-03-16
**Status:** Draft

## What We're Building

Connect the crossmatch service's LSDB/Dask workloads to a remote Dask scheduler deployed on Kubernetes, when available. If no remote scheduler is configured, fall back to Dask's default local scheduler (current behavior).

## Why This Approach

A colleague deployed a Dask cluster on the dev EKS cluster (see `dask.md`). Kubernetes exposes the scheduler address via service discovery env vars (`HOPDEVEL_DASK_SCHEDULER_SERVICE_HOST` and `HOPDEVEL_DASK_SCHEDULER_SERVICE_PORT_TCP_COMM`). To decouple the crossmatch service from Kubernetes-specific naming, we introduce a single `DASK_SCHEDULER_ADDRESS` env var that can be set from those K8s vars in the Helm chart or deployment manifest.

Using the remote Dask cluster distributes crossmatch computation across dedicated Dask workers instead of running everything inside each Celery worker process, improving throughput and resource utilization.

## Key Decisions

### 1. Single `DASK_SCHEDULER_ADDRESS` env var

Introduce `DASK_SCHEDULER_ADDRESS` (e.g., `tcp://10.0.0.5:8786`) as the service's own configuration. This decouples from Kubernetes service discovery naming. In K8s deployments, set it from the two Kubernetes env vars in the Helm values or deployment spec.

When unset or empty, the service uses Dask's default local scheduler (no `distributed.Client` is created).

### 2. Create `dask.distributed.Client` at Celery worker startup

Establish the connection once when the Celery worker process initializes via Celery's `worker_process_init` signal (fires once per prefork child process). The Client stays connected for the lifetime of the worker. LSDB automatically uses the active Dask scheduler when `.compute()` is called.

### 3. Fall back to local scheduler when `DASK_SCHEDULER_ADDRESS` is not set

For local development (docker-compose), `DASK_SCHEDULER_ADDRESS` is not set. Dask runs locally inside each Celery worker using its default synchronous scheduler (single-threaded, in-process). No changes needed to docker-compose.

### 4. No local Dask scheduler in docker-compose

Local dev does not need a Dask scheduler container. The local fallback is sufficient for development and testing.

## Open Questions

1. **Version alignment** — The Dask cluster currently runs Python 3.10. The plan is to upgrade the cluster to Python 3.12 to match the crossmatch service. The Client wiring can be implemented now, but end-to-end testing requires version alignment. Tracked separately (see `docs/brainstorms/2026-03-16-pin-python-and-deps-for-dask-cluster-brainstorm.md`).

2. **Client reconnection** — If the Dask scheduler restarts, does the `distributed.Client` reconnect automatically, or does the Celery worker need to be restarted? Need to verify Dask's reconnection behavior.

3. **Celery concurrency vs Dask** — Each Celery worker process currently runs with `CELERY_CONCURRENCY=4` (prefork). When using a remote Dask scheduler, should Celery concurrency be reduced to 1 (since Dask handles parallelism), or is it fine to have multiple Celery tasks submitting to Dask concurrently? To be decided after testing with the actual cluster.
