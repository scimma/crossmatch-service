---
title: Pin Python 3.12 and Dependencies to Match Dask Cluster
type: refactor
status: completed
date: 2026-03-16
origin: docs/brainstorms/2026-03-16-pin-python-312-and-deps-for-dask-cluster-brainstorm.md
---

# Pin Python 3.12 and Dependencies to Match Dask Cluster

## Overview

Downgrade the crossmatch-service container from Python 3.13 to Python 3.12 and pin key dependencies to match the Dask cluster on the dev EKS cluster (now upgraded to Python 3.12). Add lsdb 0.8.1 (latest) as a pinned dependency along with numpy, pandas, tornado, and dask versions that are compatible with both lsdb and the cluster.

(See brainstorm: docs/brainstorms/2026-03-16-pin-python-312-and-deps-for-dask-cluster-brainstorm.md)

## Acceptance Criteria

- [x] Dockerfile uses `python:3.12` base image (both stages) with correct `python3.12` site-packages path
- [x] `requirements.base.txt` pins `lsdb==0.8.1`, `numpy==2.4.2`, `pandas==2.3.3`, `tornado==6.5.4`, `dask[complete]>=2026.1.2`
- [x] Docker image builds successfully with all dependencies resolving
- [x] Design document updated to reflect Python 3.12

## Changes

### 1. `docker/Dockerfile`

Change Python version on 3 lines:

```dockerfile
# Line 1: deps stage
FROM python:3.12 AS deps

# Line 6: runtime stage
FROM python:3.12

# Line 15: site-packages copy path (both source and destination)
COPY --from=deps /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
```

### 2. `crossmatch/requirements.base.txt`

Replace unpinned `lsdb` with pinned versions. Add new dependencies:

```
lsdb==0.8.1
numpy==2.4.2
pandas==2.3.3
tornado==6.5.4
dask[complete]>=2026.1.2
```

- `lsdb`: pin to 0.8.1 (latest), was unpinned
- `numpy`: pin to match cluster
- `pandas`: pin to 2.3.3 (highest compatible with lsdb; nested-pandas requires <2.4)
- `tornado`: pin to match cluster (used by dask.distributed)
- `dask[complete]`: new explicit dep; `[complete]` extra brings in `distributed` for remote scheduler support

### 3. `scimma_crossmatch_service_design.md`

Update §8.1 tech stack from `Python 3.11+` to `Python 3.12`.

## Note: Cluster pandas mismatch

The colleague installed pandas==3.0.0 on the Dask cluster, but lsdb requires pandas<2.4 (via nested-pandas). The cluster workers need pandas==2.3.3 for lsdb crossmatches to work. This is a separate conversation with the colleague.

## Sources

- **Origin brainstorm:** [docs/brainstorms/2026-03-16-pin-python-312-and-deps-for-dask-cluster-brainstorm.md](docs/brainstorms/2026-03-16-pin-python-312-and-deps-for-dask-cluster-brainstorm.md) — Key decisions: Python 3.12, pin 5 packages, pandas 2.3.3 (not 3.0.0), Django 6.0 stays
- **Colleague's findings:** [dask.md](dask.md) — Cluster versions and working client setup
