---
title: Switch Container Images to GitHub Container Registry
type: refactor
status: completed
date: 2026-03-16
origin: docs/brainstorms/2026-03-16-ghcr-container-registry-brainstorm.md
---

# Switch Container Images to GitHub Container Registry

## Overview

Replace all container image references from `hub.ncsa.illinois.edu/crossmatch-service/...` with `ghcr.io/scimma/crossmatch-service`. Consolidate multiple image names into a single image since all services share the same Dockerfile.

(See brainstorm: docs/brainstorms/2026-03-16-ghcr-container-registry-brainstorm.md)

## Acceptance Criteria

- [x] All `hub.ncsa.illinois.edu` image references replaced with `ghcr.io/scimma/crossmatch-service` in docker-compose.yaml
- [x] All services in docker-compose.yaml use the same image name (no separate alert-consumer/celery names)
- [x] Helm chart `values.yaml` updated with new image repo
- [x] Docker image builds and tags correctly with new name

## Changes

### 1. `docker/docker-compose.yaml`

Replace all image references. Currently there are separate names per service — consolidate to one:

| Service | Current image | New image |
|---------|--------------|-----------|
| antares-consumer | `hub.ncsa.illinois.edu/crossmatch-service/alert-consumer:dev` | `ghcr.io/scimma/crossmatch-service:dev` |
| lasair-consumer | `hub.ncsa.illinois.edu/crossmatch-service/alert-consumer:dev` | `ghcr.io/scimma/crossmatch-service:dev` |
| celery-worker | `hub.ncsa.illinois.edu/crossmatch-service/celery:dev` | `ghcr.io/scimma/crossmatch-service:dev` |
| celery-beat | `hub.ncsa.illinois.edu/crossmatch-service/celery:dev` | `ghcr.io/scimma/crossmatch-service:dev` |
| flower | `hub.ncsa.illinois.edu/crossmatch-service/celery:dev` | `ghcr.io/scimma/crossmatch-service:dev` |

### 2. `kubernetes/charts/crossmatch-service/values.yaml`

Change `common.image.repo` (line 3):

```yaml
# Before
repo: hub.ncsa.illinois.edu/crossmatch-service/alert-consumer:dev

# After
repo: ghcr.io/scimma/crossmatch-service
```

Helm templates already use `{{ .Values.common.image.repo }}:{{ .Values.common.image.tag }}` — no template changes needed.

## Sources

- **Origin brainstorm:** [docs/brainstorms/2026-03-16-ghcr-container-registry-brainstorm.md](docs/brainstorms/2026-03-16-ghcr-container-registry-brainstorm.md) — Key decisions: replace NCSA with ghcr.io, single image name, public images, manual push
