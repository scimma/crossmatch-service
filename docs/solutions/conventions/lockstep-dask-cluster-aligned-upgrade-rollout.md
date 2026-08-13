---
title: "Rolling out a cluster-aligned dependency upgrade: lockstep tags, roll the Dask cluster first"
date: 2026-08-13
category: conventions
module: crossmatch/core/dask.py + gitops apps/dask & apps/crossmatch-service
problem_type: convention
component: development_workflow
severity: high
applies_when:
  - rolling out a dependency upgrade that must stay version-aligned between the app and its Dask cluster (lsdb/hats)
  - bumping the shared container image tag in gitops (apps/crossmatch-service common.image.tag and apps/dask image.tag are two independent pins of the same image repo)
  - promoting a version-sensitive image change to DEV or PROD via ArgoCD
  - "diagnosing a celery-worker CrashLoop reporting 'Dask version drift detected' at startup"
tags: [deployment, gitops, argocd, dask, lsdb, version-alignment, rollout-order, fail-fast]
---

# Rolling out a cluster-aligned dependency upgrade: lockstep tags, roll the Dask cluster first

## Context

The crossmatch app and its remote Dask cluster run the **same image**. The gitops
Dask chart says so directly (`apps/dask/values.yaml`):

> Same image as the crossmatch app — all Dask roles run it so python/numpy/pandas/
> dask/distributed versions match the celery worker (the Dask client) by construction.

But "same image" is a convention, not a mechanism. The two workloads pin that image
through **two independent tags**:

- the app's celery worker (the Dask *client*) via `common.image.tag` in
  `apps/crossmatch-service/values-dev.yaml` and `values-prod.yaml`;
- every Dask role (scheduler + workers) via `image.tag` in
  `apps/dask/values-dev.yaml` and `values-prod.yaml`.

Nothing in Helm couples the two tag fields. If one advances without the other, the
client runs a different image than the workers, and any version-critical dependency
skews across the Dask serialization boundary. The most dangerous of these is **lsdb**
(and its `hats` dependency): the workers execute the catalog `.compute()`, so an
app/cluster lsdb skew corrupts crossmatch results rather than just failing a pickle.

To make skew loud instead of silent, the app carries a **fail-fast startup guard** in
`crossmatch/core/dask.py`. When `DASK_SCHEDULER_ADDRESS` is set (settings.py reads it
from the env; the gitops overlays set
`dask_scheduler_address: tcp://dask-scheduler.dask.svc.cluster.local:8786`), the
celery **master** runs `verify_dask_versions` once at `worker_init` — before any fork,
where `sys.exit` actually works. It:

- compares Dask's own reported packages plus numpy/pandas across client / scheduler /
  workers via `_check_versions` (using `client.get_versions`), over
  `_VERSION_CHECK_PACKAGES`;
- separately probes the **off-boundary** packages lsdb/hats with
  `_check_off_boundary_versions`, which runs `_package_versions_local` on every worker
  via `client.run` — because `get_versions` does **not** report lsdb/hats
  (`_OFF_BOUNDARY_PACKAGES`), so `_check_versions` is blind to exactly the dependency
  that matters most.

Any drift record from either check triggers `_fail_fast()`, which SIGTERMs the parent
process so the pod exits non-zero and Kubernetes surfaces it as **CrashLoopBackOff**
(rather than an infinite billiard respawn loop inside a fork).

The friction this creates during a rollout: because the two tags are independent and
the guard fails closed, the *order* in which the app and the cluster reach the new tag
determines whether the upgrade is clean or spends time crash-looping.

## Guidance

**(1) Bump BOTH image tags in lockstep.** A dependency upgrade that must match across
the Dask boundary is not done when you edit the app's tag. In the same reviewed
commit, advance:

- `apps/crossmatch-service/values-<env>.yaml` → `common.image.tag`
- `apps/dask/values-<env>.yaml` → `image.tag`

Both overlays already carry the reminder in their comments ("Keep in lockstep with…").
The base `apps/dask/values.yaml` tag is only a transition-safe default; the effective
tag comes from the overlay. This is also the app's own convention — the fail-fast guard
and `CLAUDE.md`'s dependency-pin rule exist precisely because version skew silently
breaks distributed (de)serialization.

**(2) Roll the DASK CLUSTER FIRST, then the app.** Sequence the rollout so the Dask
scheduler + workers reach the target tag and are Running *before* the celery worker
restarts onto it. Then the app's startup guard connects to an already-aligned cluster
and passes on the **first** try, with no CrashLoop.

- **PROD** (tag-pinned, `kubectl apply`): you fully control order. Apply the Dask
  Application first, wait for the cluster to reach the target, then apply the app:

  ```
  kubectl apply -f argocd-apps/dask-prod.yaml           # roll the cluster first
  # wait: scheduler + all workers Running on the new tag (e.g. 0.11.0)
  kubectl apply -f argocd-apps/crossmatch-service-prod.yaml
  ```

  (the gitops repo's `docs/prod-promotion-runbook.md` step 3 stresses applying crossmatch-service and
  dask **together** so a partial promotion never leaves new-app-against-old-cluster.
  Dask-first is the safe refinement of "together": it satisfies the same lockstep
  requirement while also guaranteeing the guard passes on the first attempt. Both tag
  edits still ship in one reviewed commit and one release tag; only the two `kubectl
  apply` calls are ordered.)

- **DEV** (auto-sync `targetRevision: HEAD`): you *cannot* fully control order. A push
  bumps both DEV tags at once and ArgoCD auto-syncs each Application on its own; the app
  can roll ahead of the cluster. Accept that the guard may CrashLoop transiently until
  the cluster catches up (see below), or manually stage the sync (sync dask-dev first,
  wait, then let crossmatch-service-dev sync) if you want to avoid the blip.

**Related pitfall (not the main subject).** On DEV the dask app can *fail to roll at
all* if its live ArgoCD Application has stale/empty `helm.valueFiles` and silently
ignores `values-dev.yaml` — the overlay tag bump then has no effect. Fix by
re-applying the Application manifest (`kubectl apply -f argocd-apps/dask-dev.yaml`).
This is the "argocd-apps applied manually" convention: editing/committing a gitops
Application does not update the live Application; you must `kubectl apply` it.

## Why This Matters

Without the lockstep bump, the guard CrashLoops the celery worker — or, worse, in a
world without the guard, the cluster would silently mis-crossmatch on an lsdb/hats skew
that never trips the pickle layer. The lockstep bump is what keeps the two tags from
diverging in the first place.

Given lockstep, **order controls the failure mode**:

- **App-first** (or uncontrolled, as on DEV auto-sync): the guard sees a stale cluster,
  reports drift, and `_fail_fast()` fires. The pod CrashLoopBackOffs and restarts climb
  until the cluster reaches the target — a transient, self-healing blip, but a blip with
  climbing restart counts and a window where the worker is down.
- **Cluster-first** (controlled, as on PROD): the guard sees an aligned cluster and
  passes immediately. No CrashLoop, restart count stays at zero.

This session made the contrast concrete: DEV climbed to **r9** before recovering; PROD
came up clean at **r0**.

## When to Apply

Any time a dependency must stay aligned between an app and a **remote compute cluster
running the same image**, and the rollout advances two independently-pinned tags. It
matters most for dependencies **outside** the runtime version-check's covered set —
lsdb and hats are *not* in `_VERSION_CHECK_PACKAGES`; they are checked separately by
`_check_off_boundary_versions` precisely because `distributed.get_versions` omits them,
and a skew there corrupts results rather than failing loudly at the serialization
layer. Numpy/pandas/dask skew is caught too, but the off-boundary packages are the ones
that would otherwise fail *silently*, so lockstep discipline is non-negotiable for them.

Apply the same reasoning to any future app/cluster shared-image dependency (a new
crossmatch engine, a new serialization-sensitive library): bump both tags together,
roll the cluster first.

## Examples

**DEV — app rolled ahead of the cluster (app-first → CrashLoop → recover).** The gitops
push bumped both DEV tags, but ArgoCD auto-synced `crossmatch-service` ahead of `dask`.
The celery worker rolled to lsdb **0.10.4** while the Dask workers were still on
**0.9.0**. The off-boundary check reported drift:

```
Dask version drift detected drifted_packages=[
  {package: lsdb, client_version: 0.10.4, worker_versions: {..: 0.9.0}},
  {package: hats, ...}
]
```

`_fail_fast()` fired; the pod CrashLoopBackOffed and restarts climbed **r1 … r9**. Once
the Dask cluster finished rolling to **0.11.0**, the next guard run logged:

```
Dask cluster verified worker_count=2
```

and the worker recovered on its own. (Compounding this run: the dask-dev Application had
stale `helm.valueFiles` and initially ignored `values-dev.yaml`, so the cluster did not
roll until `kubectl apply -f argocd-apps/dask-dev.yaml` — see the related pitfall above.)

**PROD — cluster rolled first (dask-first → clean r0).** The `dask-prod` Application was
applied **first**; after the PROD Dask cluster (scheduler + 4 workers) reached **0.11.0**,
`crossmatch-service-prod` was applied. The celery worker's guard saw an aligned cluster
and passed on the first attempt — no CrashLoop, restart count **r0**:

```
Dask cluster verified worker_count=4
```

Same lockstep tag bump, opposite rollout order, opposite outcome: DEV's self-healing
r9 blip versus PROD's clean r0.

## Related

- `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md` — the
  pin-site half of a cluster-aligned upgrade (the four atomic pin sites in the app repo
  + local verification order). This doc is the deploy/rollout complement: same failure
  domain (app/cluster lsdb/hats skew, same `crossmatch/core/dask.py` guard), non-overlapping
  remedy. Note: that doc's claim that lsdb drift surfaces "only [via] a runtime smoke
  run" predates the `_check_off_boundary_versions` guard, which now fails fast on lsdb
  skew at startup.
- `docs/solutions/conventions/argocd-apps-applied-manually.md` — why editing/committing
  an ArgoCD Application does not update the live one (`kubectl apply` needed), which
  underpins the DEV-auto-sync vs PROD-tag-pinned rollout contrast and the stale-`valueFiles`
  pitfall above.
- `docs/solutions/design-patterns/wire-deployed-image-tag-into-footer-version.md` —
  deploy-seam sibling that threads a single `common.image.tag` through the gitops chart.
