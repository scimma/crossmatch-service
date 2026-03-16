# Brainstorm: Pin Python 3.12 and Dependencies to Match Dask Cluster

**Date:** 2026-03-16
**Status:** Draft

## What We're Building

Downgrade the crossmatch-service container from Python 3.13 to Python 3.12 and pin key dependencies to match the Dask cluster deployed on the dev EKS cluster. The cluster has been upgraded from Python 3.10 to Python 3.12.

## Why This Approach

A colleague upgraded the Dask cluster on EKS to Python 3.12 and confirmed a working client requires specific package versions. Dask uses pickle for serialization between client, scheduler, and workers, so Python and package versions must be aligned to prevent deserialization failures.

lsdb 0.8.1 (the latest version) requires pandas<2.4 via its `nested-pandas` dependency, which conflicts with the colleague's pandas==3.0.0 install. The correct pandas version for lsdb compatibility is 2.3.3. This mismatch needs to be communicated to the colleague so the cluster workers can be updated.

## Key Decisions

### 1. Downgrade Python from 3.13 to 3.12

Match the Dask cluster. Django 6.0 requires Python 3.12+, so this is compatible. The Dockerfile base image (`python:3.13`) and site-packages copy path (`python3.13`) both need updating.

### 2. Pin five packages in requirements.base.txt

| Package | Version | Rationale |
|---------|---------|-----------|
| `lsdb` | `==0.8.1` | Latest version, needed for HATS catalog crossmatching |
| `numpy` | `==2.4.2` | Matches colleague's cluster install |
| `pandas` | `==2.3.3` | Highest version compatible with lsdb (nested-pandas requires <2.4) |
| `tornado` | `==6.5.4` | Matches colleague's cluster install (used by dask.distributed) |
| `dask[complete]` | `>=2026.1.2` | Matches colleague's cluster install; resolves to 2026.1.2. The `[complete]` extra pulls in `distributed` (needed for remote Dask scheduler). Currently only a transitive dep via lsdb. |

### 3. Colleague's cluster needs pandas==2.3.3, not 3.0.0

The colleague installed pandas==3.0.0 on the cluster, but lsdb's dependency `nested-pandas` requires `pandas<2.4`. The cluster workers need to be updated to pandas==2.3.3 for lsdb crossmatches to work. This is a separate conversation.

### 4. Django 6.0 stays

Python 3.12 supports Django 6.0. No Django version change needed.

## Changes Required

1. **`docker/Dockerfile`**: Change `python:3.13` to `python:3.12` (lines 1, 6) and `python3.13` to `python3.12` (line 15 site-packages path).
2. **`crossmatch/requirements.base.txt`**: Add version pins for lsdb, numpy, pandas, tornado; add dask[complete]. Change unpinned `lsdb` to `lsdb==0.8.1`.
3. **`scimma_crossmatch_service_design.md`**: Update tech stack from "Python 3.11+" to "Python 3.12".

## Open Questions

None — all decisions made during brainstorming.
