---
date: 2026-04-20
topic: fail-fast-dask-version-check
---

# Fail-Fast Version Check Against Dask Cluster

## Problem Frame
The crossmatch-service must run identical Python, numpy, pandas, and related package versions as the remote Dask cluster (see `scimma_crossmatch_service_design.md` §7.4 — "Why exact version pinning is required"). Today, drift is only discovered when a task fails mid-flight with a confusing pickle/deserialization error somewhere deep in the Dask stack. Operators waste time diagnosing tasks that were doomed to fail before the worker even ran.

We want the service to detect drift at startup — before any alert is processed — and fail loudly with a clear diagnostic, rather than running and producing late, opaque failures.

## Requirements
- R1. The version check runs in each Celery worker process at startup (via a Celery startup signal), when `DASK_SCHEDULER_ADDRESS` is set. A failed check causes the worker process to exit non-zero, surfacing as CrashLoopBackOff in Kubernetes.
- R2. The check verifies the union of (a) Dask's default `Client.get_versions(check=True)` set — Python, distributed, dask, msgpack, cloudpickle, toolz — and (b) numpy and pandas explicitly. Comparison is exact-version equality. Drift in any of these causes the check to fail with a structured log naming each drifted package and the client/scheduler/worker versions seen.
- R3. The check waits for the cluster to be reachable AND for at least one worker to be registered before reading versions, with backoff-and-retry semantics and a maximum total wait. The max wait is configurable via an env var (e.g., `DASK_VERSION_CHECK_TIMEOUT_SECONDS`) with a sensible default (e.g., 300s). If the timeout is exceeded, the worker exits non-zero with a clear error.
- R4. The check is skipped when `DASK_SCHEDULER_ADDRESS` is unset (local Dask scheduler — no version mismatch possible).

## Constraints From Existing Architecture
- The local-dev Dask cluster installs packages at container start via `EXTRA_PIP_PACKAGES` (see `docker/docker-compose.yaml`). Pip install runs *before* the `dask-scheduler` / `dask-worker` process starts, so versions are stable by the time a component is reachable. However, workers register asynchronously after their own pip install completes — there is a meaningful window where the scheduler is up but no workers have registered yet.
- Implication: a check that requires "scheduler reachable" alone is not sufficient. The check must also confirm at least one worker is connected, because submitted tasks would otherwise fail despite the scheduler appearing healthy.
- Implication: the cluster-unreachable case is normal during startup (especially under `docker-compose up`), so first-attempt fail-fast is wrong. Backoff-with-retry until a max wait is the only viable behavior.
- Production K8s clusters are managed as a separate project (§7.4) and presumably bake versions into the image rather than installing at startup, so the install-at-startup race is dev-specific — but the dev path is where engineers will hit this first, so the design must accommodate it.

## Success Criteria
- A version drift between the service container and the Dask cluster causes the service to fail at startup with a log message naming the drifted packages, instead of failing later mid-task with a generic deserialization error.
- A correctly-aligned deployment starts cleanly with no false positives.
- The startup failure is visible in container logs and surfaces in standard Kubernetes pod status (CrashLoopBackOff / NotReady) so existing alerting catches it.

## Scope Boundaries
- Not changing the version-pinning strategy itself — see `docs/brainstorms/2026-03-16-pin-python-and-deps-for-dask-cluster-brainstorm.md` for that.
- Not auto-remediating drift (no auto-pull-and-restart, no requirements regeneration). Detect-and-fail only.
- Not building a generic infrastructure health-check framework — scoped to Dask version verification.

## Key Decisions
- **Entry point:** Celery worker startup signal. Single mechanism for dev compose and prod K8s, fails before any task is processed, no extra deployment object. Every worker re-runs the check, accepted as a small cost.
- **Allowlist:** Default `get_versions(check=True)` set + numpy + pandas, exact match. Targets the design doc's identified root causes of pickle failures (§7.4) without expanding scope to packages that don't typically cause cross-process serialization breakage.
- **Timeout:** Configurable via env var (`DASK_VERSION_CHECK_TIMEOUT_SECONDS`) with sensible default (~300s). Lets dev tune longer for cold-cache pip installs and prod tune shorter for tighter outage surfacing without code changes.

## Outstanding Questions

### Deferred to Planning
- [Affects R1][Technical] Exact Celery signal to wire into (`celeryd_init`, `worker_ready`, or a custom bootstep) and the precise mechanism for forcing a non-zero exit on failure.
- [Affects R2][Technical] Exact env name (`DASK_VERSION_CHECK_TIMEOUT_SECONDS` is a placeholder), default value, and whether to expose a separate min-workers env (currently fixed at 1).
- [Affects R2][Technical] Exact log format and structlog event name for drift detection and timeout — should match existing service logging conventions.
- [Affects R2][Needs research] Whether `Client.get_versions(check=True)` includes numpy and pandas in its output by default, or whether we need to remote-execute a tiny `client.run()` on each worker to capture them.

## Next Steps
→ `/ce:plan` for structured implementation planning.
