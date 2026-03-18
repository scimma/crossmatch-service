---
title: Simplify Helm Chart for Dev EKS Deployment
type: refactor
status: completed
date: 2026-03-18
origin: docs/brainstorms/2026-03-18-simplify-helm-chart-for-dev-eks-brainstorm.md
---

# Simplify Helm Chart for Dev EKS Deployment

## Overview

Remove production-oriented scaffolding (CloudNativePG operator, SealedSecrets, HA Valkey, node affinity, scratch volumes) from the Helm chart so it deploys cleanly on a small dev EKS cluster. Replace the operator-managed database with a simple PostgreSQL container deployment.

(See brainstorm: docs/brainstorms/2026-03-18-simplify-helm-chart-for-dev-eks-brainstorm.md)

## Acceptance Criteria

- [x] `templates/secrets.yaml` deleted
- [x] `templates/database.yaml` replaced with simple PostgreSQL Deployment + Service + PVC
- [x] `db.env` helper updated to remove CNPG `-rw` suffix from `DATABASE_HOST`
- [x] `values.yaml` Valkey section simplified: `haMode.enabled: false`, single replica
- [x] Celery-worker affinity rules (nodeAffinity + podAntiAffinity) removed from `statefulset.yaml`
- [x] Celery-worker initContainer and scratch volume mount removed from `statefulset.yaml`
- [ ] Chart deploys successfully with `helm install` on dev EKS cluster (after secrets are created)

## Changes

### 1. Delete `templates/secrets.yaml`

Remove the SealedSecrets template entirely. Secrets are created manually with `kubectl create secret generic` before deploying.

### 2. Replace `templates/database.yaml`

Replace the CNPG operator manifest with a simple PostgreSQL Deployment + Service + PVC:

```yaml
# Deployment running postgres:18.3
# Service exposing port 5432 with name from values (e.g., "django-db")
# PersistentVolumeClaim using gp2 storageClass
```

The Deployment should use:
- Image: `postgres:{{ .Values.database.imageTag | default "18.3" }}`
- Env vars: `POSTGRES_DB` and `POSTGRES_USER` from values (`.Values.database.initdb.database`, `.Values.database.initdb.username`), `POSTGRES_PASSWORD` from the `database` K8s secret (key: `password`) — matching the pattern already used in `db.env`
- Volume mount for `/var/lib/postgresql/data`
- Service name: `{{ .Values.database.clusterName }}`

### 3. Update `_helpers.yaml` — `db.env` block

Change `DATABASE_HOST` from CNPG convention to simple service name:

```yaml
# Before (CNPG convention)
- name: DATABASE_HOST
  value: {{ .Values.database.clusterName }}-rw

# After (simple service)
- name: DATABASE_HOST
  value: {{ .Values.database.clusterName }}
```

### 4. Update `values.yaml` — Valkey section

```yaml
valkey:
  fullnameOverride: "redis"
  service:
    serverPort: 6379
  haMode:
    enabled: false
    replicas: 1
```

Remove `sentinelPort` and `masterGroupName` since Sentinel is disabled.

### 5. Update `statefulset.yaml` — Remove celery-worker affinity

Remove the entire `affinity:` block (lines 115-142) from the celery-worker StatefulSet, including both `nodeAffinity` and `podAntiAffinity`.

### 6. Update `statefulset.yaml` — Remove scratch volume

Remove from the celery-worker StatefulSet:
- The `initContainers` block (volume-permissions busybox container)
- The `volumeMounts` for `job-scratch` (if present — currently only referenced in initContainer)

### 7. Add `database.imageTag` to `values.yaml`

Add to the `database` section:
```yaml
database:
  imageTag: "18.3"
```

## Deployment Verification

After changes, verify with:

```bash
# 1. Create secrets
kubectl create secret generic django \
  --from-literal=SECRET_KEY='...' \
  --from-literal=DJANGO_SUPERUSER_PASSWORD='...'
kubectl create secret generic database \
  --from-literal=password='...'
kubectl create secret generic antares \
  --from-literal=ANTARES_API_KEY='...' \
  --from-literal=ANTARES_API_SECRET='...'
kubectl create secret generic hopskotch \
  --from-literal=HOPSKOTCH_USERNAME='...' \
  --from-literal=HOPSKOTCH_PASSWORD='...'

# 2. Dry-run to check template rendering
helm template crossmatch-service kubernetes/charts/crossmatch-service \
  -f kubernetes/dev-overrides.yaml

# 3. Deploy
helm install crossmatch-service kubernetes/charts/crossmatch-service \
  -f kubernetes/dev-overrides.yaml

# 4. Verify
kubectl get pods
kubectl logs <pod-name>
```

## Sources

- **Origin brainstorm:** [docs/brainstorms/2026-03-18-simplify-helm-chart-for-dev-eks-brainstorm.md](docs/brainstorms/2026-03-18-simplify-helm-chart-for-dev-eks-brainstorm.md) — Key decisions: remove SealedSecrets, replace CNPG with simple Postgres, simplify Valkey, remove affinity/scratch
- **Current templates:** `kubernetes/charts/crossmatch-service/templates/`
- **Docker-compose reference:** `docker/docker-compose.yaml` (django-db service using `postgres:18.3`)
