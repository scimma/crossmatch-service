# Brainstorm: Switch Container Images to GitHub Container Registry

**Date:** 2026-03-16
**Status:** Draft

## What We're Building

Replace the NCSA container registry (`hub.ncsa.illinois.edu`) with GitHub Container Registry (`ghcr.io`) for all crossmatch-service container images. Consolidate multiple image names into a single image since all services share the same Dockerfile.

## Why This Approach

The project is hosted on GitHub and the team already uses GitHub for code. Using ghcr.io keeps container images co-located with the source code, simplifies access management (GitHub org permissions), and removes the dependency on the NCSA registry infrastructure.

## Key Decisions

### 1. Replace NCSA registry with ghcr.io

All image references change from `hub.ncsa.illinois.edu/crossmatch-service/...` to `ghcr.io/scimma/crossmatch-service`. The NCSA registry will no longer be used.

### 2. Single image name for all services

Currently docker-compose uses separate image names (`alert-consumer:dev`, `celery:dev`) even though all services share the same Dockerfile. Consolidate to a single image: `ghcr.io/scimma/crossmatch-service:<tag>`. Each service is differentiated by its command/entrypoint, not its image.

### 3. Public images

Images will be public on ghcr.io. No `imagePullSecrets` needed in Kubernetes. The service code is already in a public repo, so there is no additional exposure.

### 4. Manual build and push for now

No CI/CD pipeline. Developers build locally with `docker build` and push with `docker push` to ghcr.io. A GitHub Actions workflow can be added later.

## Changes Required

1. **`docker/docker-compose.yaml`**: Replace all `hub.ncsa.illinois.edu/crossmatch-service/*:dev` image references with `ghcr.io/scimma/crossmatch-service:dev`.
2. **`kubernetes/charts/crossmatch-service/values.yaml`**: Change `common.image.repo` from `hub.ncsa.illinois.edu/crossmatch-service/alert-consumer:dev` to `ghcr.io/scimma/crossmatch-service`. Helm templates already use `{{ .Values.common.image.repo }}` — no template changes needed.

## Open Questions

None — all decisions made during brainstorming.
