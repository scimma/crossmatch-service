---
title: "Atomic multi-site dependency pinning for cluster-aligned package upgrades"
date: 2026-05-12
last_updated: 2026-09-25
category: docs/solutions/conventions/
module: dependency_management
problem_type: convention
component: development_workflow
severity: medium
applies_when:
  - Upgrading any Python package pinned in requirements.base.txt / requirements.lock and docker-compose.yaml EXTRA_PIP_PACKAGES
  - Realigning local docker-compose Dask pins with the remote EKS Dask cluster
  - Performing a drop-in maintenance bump where no source-code API changes are expected
  - Upgrading a version-critical package that must match the Dask cluster (e.g., lsdb/hats), now guarded by the off-boundary startup check
  - Regenerating requirements.lock when pip-compile fails with ResolutionImpossible after a base-pin bump, or when the lock header changes unexpectedly
related_components: [tooling, testing_framework]
tags: [dependency-management, dask-cluster, docker-compose, lsdb, version-pinning, pip-compile, requirements-lock]
---

# Atomic multi-site dependency pinning for cluster-aligned package upgrades

## Context

The crossmatch service pins dependency versions in four places: the application requirements file (`requirements.base.txt`), the compiled lockfile (`crossmatch/requirements.lock`, which the runtime image is actually built from), and the `EXTRA_PIP_PACKAGES` environment variable on each of the two local docker-compose Dask services (scheduler and worker). This spread of pin sites exists because the runtime image and the local docker-compose scheduler/worker containers must all carry the same package set as the application layer, AND the same package set must match the remote Dask cluster the service offloads to. (That cluster was historically a colleague-operated EKS cluster whose environment changed out-of-band; it is now our own gitops-managed `apps/dask` deployment running the same crossmatch image, so its version is controlled by the image tag we ship — its rollout alignment is a separate convention, see Related.)

A fail-fast Dask version check at celery worker startup (`crossmatch/core/dask.py`, `_VERSION_CHECK_PACKAGES`) compares a curated list of serialization-critical packages between the local client and the connected scheduler/workers — but it does not cover every dependency. Notably, it does not cover `lsdb`. This combination — scattered pin sites, a remote cluster whose state changes out-of-band, and a version check with deliberate scope limits — creates a specific failure mode: a developer who doesn't know all four pin sites exist, or who updates them in separate commits, will either leave the local stack in a transiently broken state, ship an image built from a lockfile that drifted from the declared pins, or leave drift undetected until a runtime smoke run exercises it.

## Guidance

1. **Know the four pin sites and update them atomically.** The four locations that must move together in a single commit are:
   - `crossmatch/requirements.base.txt` — the application-layer pip pin (the human-edited source of truth).
   - `crossmatch/requirements.lock` — the compiled lockfile, regenerated from the base file with `pip-compile --strip-extras --output-file=requirements.lock requirements.base.txt`. **The runtime image is built from this file** — `docker/Dockerfile` installs `requirements.lock`, not `requirements.base.txt` — and a lock that drifts from the base file fails a dedicated lock-drift CI check, so it must be regenerated in the same commit as any base-pin change.
   - `docker/docker-compose.yaml`, `dask-scheduler` service, `EXTRA_PIP_PACKAGES` string — the local scheduler container pin.
   - `docker/docker-compose.yaml`, `dask-worker` service, `EXTRA_PIP_PACKAGES` string — the local worker container pin.

   Splitting these across commits is unsafe: the local docker-compose stack's fail-fast version check fires at celery worker startup against whichever pins are currently in tree, so a commit that updates the application pin but not the container pins (or vice versa) will trip the check and fail until the remaining sites are updated.

2. **Understand the two startup checks and what each covers.** `_VERSION_CHECK_PACKAGES` in `crossmatch/core/dask.py` compares `python`, `distributed`, `dask`, `msgpack`, `cloudpickle`, `toolz`, `tornado`, `numpy`, `pandas` via `client.get_versions()`. That call does not report `lsdb`/`hats` (they sit above the Dask serialization boundary), so a *separate* off-boundary check — `_check_off_boundary_versions`, added in the 0.10.4 upgrade — queries each worker's `lsdb`/`hats` version via `client.run` and fails fast on skew. Between the two, both the serialization-critical set AND `lsdb`/`hats` now surface at worker startup; only a package in neither set would go undetected until the smoke run. Before upgrading, determine which check (if any) covers the package.

3. **Follow the verification path in order, and treat the smoke run as load-bearing.**
   1. Confirm pip can resolve the new pin without conflict. Run inside Python 3.12 — a venv or a container. Host Python 3.10 fails pip resolution because `django>=6.0,<6.1` in `crossmatch/requirements.base.txt` requires Python 3.12+.
   2. Start the local docker-compose Dask stack (scheduler + worker) and confirm clean startup with no version-check failures.
   3. Run the pytest suite (`python -m pytest`, in-container per `docs/developer.md`) as a low-cost sanity check, but do not treat a clean result as meaningful signal for *dependency alignment* — the unit tests exercise app logic, not the remote Dask serialization round-trip, so the smoke run (step 5) is what actually catches version drift.
   4. Start a celery worker against the remote EKS cluster and confirm the fail-fast check reports all compared packages aligned.
   5. Run a single-alert end-to-end smoke run against the hosted HATS catalogs. This is the only verification surface that exercises the full round-trip including any packages outside the fail-fast scope.
   6. **Before PROD, replay historical alerts on DEV** (`docs/runbooks/crossmatch-replay.md`): take a baseline snapshot of a PROD alert sample before the DEV rollout, a candidate snapshot after it, and compare them. The upgrade is ready for PROD when every difference group in the report is explained in the gitops PROD promotion MR (not the app PR: images are built only from release tags, so the app PR merges before a candidate replay can exist). This replaces a clean live DEV batch as the load-bearing gate: it needs no live alerts, and it compares what the service would *publish*, which a smoke run that merely completes cannot show.

4. **Regenerate the lock the way CI does, and expect transitive caps to block a major bump.** Two things about `pip-compile` are not visible from the lock itself:
   - **It treats existing lock pins as preferences, so a transitive package that caps the moved package fails resolution.** A plain recompile keeps every other package at its locked version. If one of them declares an upper bound on the package you are moving, `pip-compile` reports `ResolutionImpossible` instead of moving that transitive package. Find the capping package from the conflict details, confirm on PyPI that a newer release lifts the cap, and move only that package:

     ```bash
     cd crossmatch
     pip-compile --strip-extras --upgrade-package <capping-package> \
       --output-file=requirements.lock requirements.base.txt
     ```

     Do not reach for a blanket `--upgrade`: it moves the whole transitive tree, and every package that moves is another version the Dask cluster must match. Then read the lock diff and confirm that it contains only the packages you intended to move plus the capping package. Record the forced transitive move in the CHANGELOG and the PR, because the base file does not show it.
   - **The lock header records `pip-compile`'s own options, and they vary with the pip-tools version.** The `lock-drift` check (`.github/workflows/lock-drift.yml`) installs `"pip<26.2" pip-tools`, which gives the newest pip-tools under that pip cap, then recompiles and fails on any byte difference, header included. A lock compiled with a different pip or pip-tools can differ from CI's only in the header comment and still fail the check. Regenerate it in a Python 3.12 venv that uses the same install line as the workflow. If the header changes anyway, that is a pip-tools release changing its output; the check on the PR is the authority on whether it matches.

5. **Determine whether source-code edits are needed before touching the pins.** For drop-in maintenance bumps (patch or minor releases with no API changes), check the upstream release notes and diff the call sites in this repo. The two `lsdb` call sites today are `crossmatch/matching/catalog.py` (`lsdb.open_catalog`) and `crossmatch/tasks/crossmatch.py` (`lsdb.from_dataframe`). If the public signatures those call sites use are unchanged, the upgrade is a pure pin bump with no code edits. If signatures or behaviors changed, plan source edits alongside the pin bump and note the scope expansion in the commit message.

## Why This Matters

If pins are updated non-atomically, the fail-fast version check fails between commits and blocks any developer who pulls mid-upgrade — the local docker-compose stack will refuse to start until the remaining pin sites catch up.

If pins are updated in the wrong subset of the four sites, the application layer and the container layer diverge, producing confusing behavior where the containers run a different version than the application expects.

If `requirements.base.txt` moves but `crossmatch/requirements.lock` is not regenerated, two things go wrong: the lock-drift CI check fails the branch, and — because `docker/Dockerfile` installs from the lock — the built runtime image silently lags the declared pins until the lock is recompiled. Regenerate the lock in the same commit.

`lsdb`/`hats` skew between the app and the Dask cluster used to surface only during actual crossmatch tasks — as pickle deserialization errors, unexpected `AttributeError`s, or silent result corruption — which is why the smoke run was the load-bearing gate. As of the 0.10.4 upgrade, the off-boundary startup check (`_check_off_boundary_versions`) catches `lsdb`/`hats` skew at celery-worker startup (fail-fast -> CrashLoopBackOff), so it no longer ships silently. The smoke run still matters — it is the only surface that exercises the full compute round-trip — but it is no longer the *only* thing standing between an lsdb skew and production.

## When to Apply

- When bumping any package that appears in `EXTRA_PIP_PACKAGES` in `docker/docker-compose.yaml`, regardless of whether it is in the fail-fast check scope.
- When upgrading a package whose version must align with the remote Dask cluster (now our own gitops `apps/dask`, running the same image) — advance the cluster's image tag in lockstep with the app's, and roll the cluster before the app (see the lockstep-rollout convention in Related).
- On drop-in maintenance bumps (no expected API changes) and on bumps that carry breaking API changes alike — the distinction affects whether source-code edits accompany the pin bump, not whether the three-site atomic pattern applies.
- When the remote cluster has already been upgraded and the local pins are lagging (the "catch up" case) — same atomicity requirement.

## Examples

### LSDB 0.8.1 → 0.9.0 upgrade (branch `refactor/lsdb-upgrade-0.9.0`, 2026-05-12)

Three pin sites updated in one atomic commit (this upgrade predates `crossmatch/requirements.lock`; the same bump today would also regenerate the lock, making it four):

- `crossmatch/requirements.base.txt` line 12: `lsdb==0.8.1` → `lsdb==0.9.0`
- `docker/docker-compose.yaml` line 353 (`dask-scheduler` `EXTRA_PIP_PACKAGES`): `"lsdb==0.8.1 numpy==2.4.2 pandas==2.3.3 s3fs"` → `"lsdb==0.9.0 ..."`
- `docker/docker-compose.yaml` line 372 (`dask-worker` `EXTRA_PIP_PACKAGES`): identical string updated

Pre-upgrade research confirmed that `lsdb.open_catalog` and `lsdb.from_dataframe` signatures were byte-identical between v0.8.1 and v0.9.0 (verified against the upstream `https://github.com/astronomy-commons/lsdb` release tags). No edits to `crossmatch/matching/catalog.py` or `crossmatch/tasks/crossmatch.py` were needed.

The `lsdb` package is not in `_VERSION_CHECK_PACKAGES`, so the fail-fast check at celery startup would not have detected drift had the pins been left mismatched. The smoke run against the three hosted HATS catalogs (DES Y6 Gold, DELVE DR3 Gold, SkyMapper DR4) was the gate that confirmed correctness.

Verification outcomes:

1. Pip resolution in a Python 3.12 venv — clean, no conflicts.
2. Local docker-compose stack — scheduler and worker came up cleanly with the updated `EXTRA_PIP_PACKAGES`.
3. `manage.py test` — found zero tests (Django's default runner does not discover this project's suite). **Note: a unit-test result is not load-bearing for a dependency bump.** A real pytest/pytest-django suite now exists under `crossmatch/tests/` and runs in-container (per `docs/developer.md`), but even a green run cannot reveal cluster version drift — the unit tests exercise app logic, not the remote Dask serialization round-trip. The meaningful verification surfaces for an upgrade are the docker-compose startup, the fail-fast Dask check, and the end-to-end smoke run.
4. Celery worker started against the remote EKS cluster — fail-fast check reported all compared packages aligned.
5. Single-alert end-to-end smoke run — returned sensible crossmatch results against all three catalogs with no pickle exceptions.

### LSDB 0.10.4 → 0.11.0 upgrade (scimma/crossmatch-service#112, 2026-09-24)

lsdb 0.11.0 requires hats 0.11 and nested-pandas 0.7, and both require `pandas>=3`, so the upgrade also moved pandas from 2.3.3 to 3.0.6. After the base pins moved, a plain recompile failed with `ResolutionImpossible`: the locked `db-dtypes==1.6.0`, which comes in through `pittgoogle-client`, declares `pandas<3.0.0`. `db-dtypes` 1.7.1 allows `pandas<4`, so `pip-compile --upgrade-package db-dtypes` resolved it. The lock diff was exactly five packages: db-dtypes, hats, lsdb, nested-pandas and pandas. numpy, pyarrow and dask did not move.

The regenerated lock header changed from `pip-compile --output-file=...` to `pip-compile --no-index --output-file=...`. The regeneration ran under pip-tools 7.6.1, and the previous lock dated from pip-tools 7.6.0; nothing else about the compile changed. Regenerated with the CI install line, the lock recompiled byte-for-byte, and `lock-drift` passed on the PR.

## Related

- `docs/brainstorms/2026-05-12-upgrade-lsdb-to-0-9-0-requirements.md` — requirements doc that drove the upgrade; states the fail-fast check's lsdb gap and the smoke run's load-bearing role explicitly.
- `docs/plans/2026-05-12-001-refactor-lsdb-upgrade-0-9-0-plan.md` — executed plan; definitive line-numbered record of the three pin sites and the full verification checklist.
- `docs/brainstorms/2026-04-20-fail-fast-dask-version-check-requirements.md` — background on why version drift causes opaque pickle failures.
- `docs/plans/2026-04-20-001-feat-fail-fast-dask-version-check-plan.md` — specifies `_VERSION_CHECK_PACKAGES`, the install-at-startup race that motivates the ≥1-worker wait, and why `lsdb` is intentionally absent from the checked set.
- `docs/brainstorms/2026-03-19-local-dask-scheduler-docker-compose-brainstorm.md` — formalizes `EXTRA_PIP_PACKAGES` as the local Dask worker pin site and explains the rationale (workers need packages for pickle deserialization; the scheduler does not strictly require all of them but carries the same set for symmetry).
- `docs/brainstorms/2026-03-16-pin-python-312-and-deps-for-dask-cluster-brainstorm.md` — introduced `lsdb` as a pinned dependency and established the two-pin-site pattern.
- `crossmatch/core/dask.py` (`_VERSION_CHECK_PACKAGES` and `_check_off_boundary_versions` / `_OFF_BOUNDARY_PACKAGES`) — live code for the two startup checks: the serialization-critical set via `get_versions`, and the off-boundary `lsdb`/`hats` check via `client.run` added in the 0.10.4 upgrade.
- `docs/solutions/conventions/lockstep-dask-cluster-aligned-upgrade-rollout.md` — the deploy/rollout complement to this pin-site doc: bump the app and gitops `apps/dask` image tags in lockstep and roll the Dask cluster before the app so the off-boundary guard passes on the first try.
- `docs/runbooks/crossmatch-replay.md` — the replay flow (export on PROD, baseline and candidate replay on DEV, compare) that step 6 above names as the pre-PROD gate.
