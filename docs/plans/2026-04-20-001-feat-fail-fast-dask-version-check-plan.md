---
title: "feat: Fail-fast Dask cluster version-drift check at worker startup"
type: feat
status: completed
date: 2026-04-20
origin: docs/brainstorms/2026-04-20-fail-fast-dask-version-check-requirements.md
---

# feat: Fail-fast Dask cluster version-drift check at worker startup

## Overview

Replace the current silent-fallback Dask connection in `crossmatch/core/dask.py` with a fail-fast version-drift check. When `DASK_SCHEDULER_ADDRESS` is set, each Celery worker process at startup will: (1) wait for the cluster to be reachable AND ≥1 Dask worker registered (with backoff up to a configurable timeout), (2) inspect `Client.get_versions()` output for drift in the default Dask package set plus numpy/pandas, and (3) crash the Celery worker process non-zero on drift or timeout — surfacing as CrashLoopBackOff in Kubernetes.

This replaces the failure mode where drift is only discovered mid-task with confusing pickle errors (see origin: `docs/brainstorms/2026-04-20-fail-fast-dask-version-check-requirements.md`).

## Problem Frame

Today, `crossmatch/core/dask.py` connects to the remote scheduler and silently falls back to the local synchronous scheduler on any exception. Combined with §7.4 of the design doc — "Why exact version pinning is required (no escape hatch)" — this means version drift between the client and the cluster is only discovered when a Celery task fails mid-flight with an opaque pickle/deserialization error somewhere deep in the Dask stack. Operators waste time diagnosing tasks that were doomed before the worker even ran.

## Requirements Trace

- R1. Version check runs in each Celery worker process at startup (via `worker_process_init` signal). Failed check causes process to exit non-zero.
- R2. Check covers Dask's default `Client.get_versions(check=True)` package set (Python, distributed, dask, msgpack, cloudpickle, toolz, tornado) plus numpy and pandas explicitly. Exact-version equality. Drift produces a structured log naming each drifted package and the client/scheduler/worker versions seen.
- R3. Check waits for cluster reachable AND ≥1 Dask worker registered before reading versions. Backoff-and-retry within a max wait configurable via `DASK_VERSION_CHECK_TIMEOUT_SECONDS` env var (default 300s). Timeout exceeded → Celery worker process exits non-zero.
- R4. Check skipped when `DASK_SCHEDULER_ADDRESS` is unset.

## Scope Boundaries

- Not changing the version-pinning strategy — see `docs/brainstorms/2026-03-16-pin-python-and-deps-for-dask-cluster-brainstorm.md`.
- Not auto-remediating drift (no auto-pull-and-restart, no requirements regeneration).
- Not building a generic infrastructure health-check framework — scoped to Dask version verification.
- Not introducing a test framework. The repo currently has no `pytest`, `conftest.py`, or test files. Adding one would be an independent infrastructure decision.

### Deferred to Separate Tasks

- Test coverage for the version-check logic: deferred until the project introduces a test infrastructure pattern.

## Context & Research

### Relevant Code and Patterns

- `crossmatch/core/dask.py` — current implementation. The new logic replaces `connect_dask_scheduler()` in this file. Existing pattern: `@worker_process_init.connect` decorator on a function taking `**kwargs`. Existing logging style: `logger.info('Connecting to remote Dask scheduler', address=address)`.
- `crossmatch/project/celery.py` — already imports `from core import dask  # noqa: F401` to register the signal handler. No change needed there.
- `crossmatch/project/settings.py:15` — `DASK_SCHEDULER_ADDRESS = os.getenv('DASK_SCHEDULER_ADDRESS', '')` is already defined. Add the new timeout env var adjacent to it.
- `crossmatch/project/settings.py:56-58` — `CROSSMATCH_BATCH_MAX_WAIT_SECONDS = int(os.getenv('CROSSMATCH_BATCH_MAX_WAIT_SECONDS', '900'))` shows the int-with-default pattern.
- `crossmatch/core/log.py` — `structlog.get_logger(__name__)` factory, used everywhere.
- `docker/docker-compose.yaml:331-364` — the local-dev Dask cluster (image `ghcr.io/dask/dask:2026.1.2-py3.12`, `EXTRA_PIP_PACKAGES: lsdb==0.8.1 numpy==2.4.2 pandas==2.3.3 s3fs`). Worker registers asynchronously after its own pip install completes — primary motivator for the "≥1 worker registered" wait.
- `scimma_crossmatch_service_design.md` §7.4 — "Why exact version pinning is required (no escape hatch)" — this plan delivers operational mitigation #2 from that section.

### External References

- `dask.distributed.Client.get_versions(check=True)` — defaults already include numpy, pandas, lz4. **Important**: `check=True` emits `warnings.warn()` (UserWarning), it does NOT raise. The plan must inspect the returned dict directly rather than relying on the warning side effect. Source: `distributed/versions.py` and `distributed/client.py`.

## Key Technical Decisions

- **Inspect the returned dict, don't rely on warnings.** Because `Client.get_versions(check=True)` only `warnings.warn()` on mismatch instead of raising, we call `client.get_versions(check=False)` and walk the structure ourselves. This gives us full control over which packages count as drift, what we log, and how we exit. Using `check=False` also avoids the edge case where a `-W error` python flag would coerce the warning into an unhandled exception inside Dask's sync wrapper.
- **Worker exit mechanism: raise `celery.exceptions.WorkerShutdown`.** `worker_process_init` fires inside a billiard-forked child, not the Celery master. `WorkerShutdown` is Celery's documented API for cleanly shutting down a worker from inside a signal handler — it propagates to the master and causes the worker to exit. Alternatives considered: `sys.exit(1)` (billiard may catch and respawn the child without propagating); `os._exit(1)` (works but skips Python finalizers and is heavier-handed than necessary). The verification step in Unit 2 must explicitly confirm the master process exits non-zero, not just the forked child.
- **Wait loop uses two distinct conditions.** "Connection succeeds" and "≥1 worker registered" are checked separately so that the structured log can clearly identify which condition is still pending when the timeout fires.
- **One env var, not two.** Only `DASK_VERSION_CHECK_TIMEOUT_SECONDS` is exposed. The min-worker count is fixed at 1 in code — exposing it would invite over-tuning and complicate the failure modes. If we ever need to require N workers, that's a follow-up.

## Open Questions

### Resolved During Planning

- **Does `get_versions(check=True)` cover numpy/pandas?** Yes — both are in the default `optional_packages` list. No need to pass `packages=[...]`.
- **What does `check=True` raise on mismatch?** Nothing — it only emits `warnings.warn(UserWarning, ...)`. The returned dict must be inspected directly. Use `check=False` to also avoid the `-W error` edge case.
- **Which Celery signal?** `worker_process_init` (matches existing pattern in `crossmatch/core/dask.py`; fires per child process; the LSDB client lives per-process).
- **How to wait for ≥1 worker?** Use `client.wait_for_workers(n_workers=1, timeout=remaining_seconds)` — Dask's purpose-built API. Avoid hand-rolled polling on `client.scheduler_info()` because the default `n_workers=5` truncates and misreports the worker count.

- **Worker exit mechanism** — Resolved: raise `celery.exceptions.WorkerShutdown` (Celery's documented API for shutting down a worker from inside a signal handler). Verified at implementation time per the verification step in Unit 2.

### Deferred to Implementation

- **Exact backoff curve** (e.g., exponential 1s→2s→4s→8s capped at 10s, vs constant 5s polling). Default to exponential with a 10s cap to balance startup speed against polite cluster polling. Implementer may adjust for clarity. Total budget is `DASK_VERSION_CHECK_TIMEOUT_SECONDS`.

## Implementation Units

- [x] **Unit 1: Add `DASK_VERSION_CHECK_TIMEOUT_SECONDS` setting**

**Goal:** Expose the version-check timeout as a configurable environment variable with a sensible default.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Modify: `crossmatch/project/settings.py`

**Approach:**
- Add `DASK_VERSION_CHECK_TIMEOUT_SECONDS = int(os.getenv('DASK_VERSION_CHECK_TIMEOUT_SECONDS', '300'))` adjacent to the existing `DASK_SCHEDULER_ADDRESS` line (around line 15-16).
- Match the `int(os.getenv(...))` cast pattern already used by `CROSSMATCH_BATCH_MAX_WAIT_SECONDS`.

**Patterns to follow:**
- `crossmatch/project/settings.py` lines 56-58 — int env var with default.

**Test scenarios:**
- Test expectation: none — pure config addition with no behavioral logic.

**Verification:**
- `python -c "from django.conf import settings; print(settings.DASK_VERSION_CHECK_TIMEOUT_SECONDS)"` returns `300` when the env var is unset and respects the env var when set.

---

- [x] **Unit 2: Replace silent-fallback connect with fail-fast version check**

**Goal:** Rewrite `connect_dask_scheduler()` in `crossmatch/core/dask.py` to perform the version-drift check at worker startup and exit the worker process on failure.

**Requirements:** R1, R2, R3, R4

**Dependencies:** Unit 1

**Files:**
- Modify: `crossmatch/core/dask.py`

**Approach:**
- Keep the `@worker_process_init.connect` decorator on the existing function.
- When `settings.DASK_SCHEDULER_ADDRESS` is empty: log "No DASK_SCHEDULER_ADDRESS set, using local Dask scheduler" and return (preserves R4 and current behavior).
- When set: enter a connection retry loop that polls until `time.monotonic()` exceeds the deadline derived from `settings.DASK_VERSION_CHECK_TIMEOUT_SECONDS`. Inside the loop, try to construct `Client(address)` — on connection error, log at DEBUG ("waiting for Dask scheduler") and sleep before next iteration.
- Once `Client(address)` succeeds, call `client.wait_for_workers(n_workers=1, timeout=remaining_seconds)` — Dask's purpose-built API for waiting on worker registration. It blocks until ≥1 worker registers or `TimeoutError` fires when `remaining_seconds` elapses. Prefer this over a custom poll on `client.scheduler_info()` (which has an `n_workers=5` default that would also misreport `worker_count` in the success log if used).
- Once a worker is registered, call `versions = client.get_versions(check=False)` and inspect the returned dict.
- Drift detection: for each package in { `python`, `distributed`, `dask`, `msgpack`, `cloudpickle`, `toolz`, `tornado`, `numpy`, `pandas` }, compare `versions['client']['packages'][package]` against `versions['scheduler']['packages'][package]` and against each entry in `versions['workers'][<addr>]['packages'][package]`. (These are Dask's defaults; no extra `packages=` arg needed. Lowercase keys per `distributed/versions.py`.) Collect mismatches into records like `{package, client_version, scheduler_version, worker_versions: {addr: ver, ...}}`.
- For accurate worker count in success/failure logs, query `client.scheduler_info(n_workers=-1)['workers']` (the default `n_workers=5` truncates).
- On drift: emit `logger.error('Dask version drift detected', scheduler_address=..., drifted_packages=[{package, client_version, scheduler_version, worker_versions}, ...])` then `raise celery.exceptions.WorkerShutdown` (see Key Technical Decisions).
- On timeout: emit `logger.error('Dask version check timed out', scheduler_address=..., reason=..., timeout_seconds=..., elapsed_seconds=...)` where `reason` is `"scheduler unreachable"` (Client construction never succeeded) or `"no workers registered"` (Client constructed but `wait_for_workers` raised TimeoutError). Then `raise celery.exceptions.WorkerShutdown`.
  - Caveat: once `Client()` succeeds, Dask's `_update_scheduler_info` swallows `OSError` on subsequent calls — so a scheduler that dies after construction will degrade to the "no workers registered" reason rather than "scheduler unreachable". Acceptable; both still exit non-zero.
- On success: emit `logger.info('Dask cluster verified', scheduler_address=..., worker_count=N, elapsed_seconds=...)` and return. The `Client` remains registered as the default scheduler for this process.
- Remove the existing `try / except / fall back to local` block — that behavior is now incompatible with R1.

**Patterns to follow:**
- Existing structlog event names in this file: imperative or noun phrase, sentence-cased, key=value kwargs in snake_case.
- Existing import-locality pattern: `from dask.distributed import Client` inside the function (preserves the cold-import benefit).

**Test scenarios:**
- Test expectation: none — see `### Deferred to Separate Tasks`. Manual integration test via local docker-compose described under Verification.

**Verification:**
- With `docker-compose --profile dask-scheduler up`, the celery worker connects, logs `Dask cluster verified` with a worker count and elapsed seconds, and proceeds to accept tasks.
- Forcing version drift (e.g., setting `EXTRA_PIP_PACKAGES: numpy==2.3.0` on the worker while keeping `numpy==2.4.2` on the scheduler) causes the celery worker to log `Dask version drift detected` with the drifted package list (each entry showing client/scheduler/worker versions), then exit non-zero. `docker compose ps` shows the celery worker as `exited (1)`.
- Confirm the structured error log is visible on the celery-worker container's stdout (i.e., that `worker_process_init` runs after Celery's `CELERYD_REDIRECT_STDOUTS` has been set up and structlog output reaches the pod log stream). If it isn't, configure structlog explicitly in `crossmatch/core/log.py` so events are processed before being written.
- Stopping the dask-scheduler service before the celery worker starts (so it can't reach the cluster) causes the celery worker to retry, then log `Dask version check timed out` with `reason=scheduler unreachable` after `DASK_VERSION_CHECK_TIMEOUT_SECONDS` and exit non-zero.
- Unsetting `DASK_SCHEDULER_ADDRESS` makes the worker log `No DASK_SCHEDULER_ADDRESS set` and start normally without contacting any cluster.
- Confirm that the chosen exit mechanism (see Key Technical Decisions) actually causes the celery master process to exit non-zero, not just the forked child — `docker compose up` exits the container with non-zero status, K8s reports CrashLoopBackOff. If the master respawns the child instead, the chosen mechanism is wrong and must be revisited.

---

- [x] **Unit 3: Expose `DASK_VERSION_CHECK_TIMEOUT_SECONDS` in deployment config**

**Goal:** Make the new timeout env var configurable from the same surfaces as `DASK_SCHEDULER_ADDRESS` — `.env.example`, `docker-compose.yaml`, and the Helm chart values + helper template — so operators can tune it without code changes.

**Requirements:** R3

**Dependencies:** Unit 1

**Files:**
- Modify: `docker/.env.example`
- Modify: `docker/docker-compose.yaml` (celery-worker service env)
- Modify: `kubernetes/charts/crossmatch-service/values.yaml`
- Modify: `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`

**Approach:**
- Add `DASK_VERSION_CHECK_TIMEOUT_SECONDS=` to `docker/.env.example` adjacent to the existing `DASK_SCHEDULER_ADDRESS` entry, with a comment noting it defaults to 300s and may need bumping for cold `EXTRA_PIP_PACKAGES` installs.
- In `docker/docker-compose.yaml`, add `DASK_VERSION_CHECK_TIMEOUT_SECONDS: "${DASK_VERSION_CHECK_TIMEOUT_SECONDS:-600}"` to the celery-worker service env block. Default 600 (not 300) for compose specifically — cold pip install of `lsdb + numpy + pandas + s3fs` on a fresh image cache regularly exceeds 300s.
- In `kubernetes/charts/crossmatch-service/values.yaml` under the `celery` block (where `dask_scheduler_address` already lives), add `dask_version_check_timeout_seconds: 300`.
- In `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`, add the new env var to the helper that already exposes `DASK_SCHEDULER_ADDRESS` (around line 36), wired to `{{ .Values.celery.dask_version_check_timeout_seconds }}`.

**Patterns to follow:**
- `kubernetes/charts/crossmatch-service/templates/_helpers.yaml` lines 36-37 — existing `DASK_SCHEDULER_ADDRESS` exposure is the precedent.
- `docker/docker-compose.yaml` celery-worker env block — existing `DASK_SCHEDULER_ADDRESS` line is the precedent.

**Test scenarios:**
- Test expectation: none — pure config plumbing.

**Verification:**
- Setting `DASK_VERSION_CHECK_TIMEOUT_SECONDS=120` in a local `.env` flows through to the celery-worker container and the timeout fires after 120s in the "scheduler unreachable" verification scenario from Unit 2.
- `helm template` rendering with a values override sets the env var on the celery-worker container spec.

---

- [x] **Unit 4: Update design doc §7.4 to reference the new check**

**Goal:** Note in the design doc that operational mitigation #2 from the "Why exact version pinning is required" subsection has been implemented.

**Requirements:** Documentation accuracy (in scope: keeping design and implementation consistent)

**Dependencies:** Unit 2, Unit 3

**Files:**
- Modify: `scimma_crossmatch_service_design.md`

**Approach:**
- In §7.4, find the bullet describing operational mitigation #2 ("call `Client.get_versions(check=True)` at startup to fail fast on drift instead of mid-task") and add a parenthetical or follow-up sentence indicating the check now lives in `crossmatch/core/dask.py` and is governed by `DASK_VERSION_CHECK_TIMEOUT_SECONDS`.
- Add `DASK_VERSION_CHECK_TIMEOUT_SECONDS` to the env-var table in §10 if one exists; otherwise leave inline.

**Test scenarios:**
- Test expectation: none — documentation update.

**Verification:**
- The §7.4 mitigation bullet now points to the implementation file and env var. A reader following the bullet can find the actual code without grepping.

## System-Wide Impact

- **Behavior change:** Today a typo in `DASK_SCHEDULER_ADDRESS` silently falls back to local Dask. After this change, a typo crashes the worker. This is intentional (the fail-fast goal applies to all configuration drift, not only version drift) — but worth noting in PR description so reviewers and operators are aware.
- **Startup latency:** Each Celery worker process now blocks for up to `DASK_VERSION_CHECK_TIMEOUT_SECONDS` (default 300s) before accepting tasks. Affects rolling-restart behavior and the time before CrashLoopBackOff fires.
- **Per-process repetition:** `worker_process_init` fires per forked child process. Concurrency=N replicas=R means N×R independent checks per startup wave. Each check is a Client connect + a `get_versions` broadcast (one RPC per Dask worker) + `wait_for_workers`. At current scale (`CELERY_CONCURRENCY=4`, low replica count, modest Dask cluster) the load is negligible. At larger scale (e.g., 100 Dask workers × 50 Celery worker forks during a rolling restart), the broadcasts could briefly stall the single-threaded Dask scheduler. Accepted for now; revisit if scale grows or if the per-startup latency spikes show up in monitoring. A future per-pod check (e.g., K8s init container or a single-flight cache file) would be the mitigation.
- **Unchanged invariants:** `crossmatch/project/celery.py` still imports `core.dask` to register the signal — no change to celery app construction or task registration. LSDB call sites (`crossmatch/matching/catalog.py:54`) continue to use the implicit default registered client.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Cold-cache `EXTRA_PIP_PACKAGES` install on dev compose exceeds default 300s timeout | Unit 3 sets the compose-specific default to 600s via `DASK_VERSION_CHECK_TIMEOUT_SECONDS` in `docker-compose.yaml`. Engineers on warmer caches can lower it via `.env`. Production K8s default stays at 300s since cluster images bake versions in. |
| `Client.get_versions(check=True)` semantics change in a future Dask release (e.g., starts actually raising) | We don't depend on the raising behavior — we inspect the returned dict directly. A defensive `try/except` around the `client.get_versions()` call covers any unexpected raise without changing semantics. |
| Network blip between the celery worker pod and the Dask scheduler during initial check window | The retry loop absorbs transient unreachability up to the timeout. After startup, network blips are handled by Dask's own client logic, not this code. |
| Future need to require N>1 workers before declaring the cluster ready | Out of scope. If raised later, expose a second env var rather than overloading this one. |

## Documentation / Operational Notes

- The deployment-config plumbing (`docker/.env.example`, `docker-compose.yaml`, Helm values + helper template) is now Unit 3 — it is first-class implementation work, not an aside.
- The new behavior should be called out in the PR description under "behavior change": typo'd or unreachable `DASK_SCHEDULER_ADDRESS` now crashes the worker rather than silently falling back to local Dask. Operators should be made aware before the change is rolled out.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-20-fail-fast-dask-version-check-requirements.md](docs/brainstorms/2026-04-20-fail-fast-dask-version-check-requirements.md) — carries forward all three key decisions (entry point, allowlist, configurable timeout) and the architectural constraints around `EXTRA_PIP_PACKAGES`.
- **Design doc context:** `scimma_crossmatch_service_design.md` §7.4 — establishes why exact pinning is required and identifies fail-fast version checking as operational mitigation #2.
- **Existing implementation to replace:** `crossmatch/core/dask.py`
- **Celery signal precedent:** `crossmatch/project/celery.py` (already imports `core.dask` to register the worker_process_init signal)
- **Dask source:** `distributed/distributed/versions.py` (default package list including numpy/pandas) and `distributed/distributed/client.py` (`get_versions` semantics — `check=True` warns, does not raise)
