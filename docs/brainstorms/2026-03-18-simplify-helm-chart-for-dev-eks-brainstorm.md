# Brainstorm: Simplify Helm Chart for Dev EKS Deployment

**Date:** 2026-03-18
**Status:** Draft

## What We're Building

Strip production-oriented scaffolding from the Helm chart so it can deploy cleanly onto a small dev EKS cluster. The chart was originally scaffolded with production patterns (CloudNativePG operator, SealedSecrets, HA Valkey, node affinity) that aren't available or needed on the dev cluster.

## Why This Approach

This is the first Kubernetes deployment of the crossmatch-service. The dev EKS cluster is small and doesn't have the operators or node topology the chart assumes. Simplifying now unblocks deployment; production patterns can be reintroduced later when deploying to a production cluster.

## Key Decisions

### 1. Remove `secrets.yaml` (SealedSecrets)

The template uses Bitnami SealedSecrets with encrypted values sealed for a different cluster. The dev cluster doesn't have the SealedSecret controller. Secrets will be created manually with `kubectl create secret generic` before deploying. The `_helpers.yaml` templates already reference secrets by name via `secretKeyRef`, so manual secrets work without any template changes.

### 2. Replace `database.yaml` with a simple PostgreSQL Deployment

The current template uses the CloudNativePG operator (`postgresql.cnpg.io/v1`), which isn't installed on the dev cluster. Replace with a simple Deployment running `postgres:18.3` (matching docker-compose) plus a Service, with a PersistentVolumeClaim using `gp2` storage. The `db.env` helper currently sets `DATABASE_HOST` to `{{ .Values.database.clusterName }}-rw` (the `-rw` suffix is a CNPG convention). This needs updating to match the new simple Service name. Eventually the EKS cluster will use an externally provisioned RDS instance, never a dynamically created operator-managed cluster.

### 3. Simplify Valkey to single replica, no Sentinel

The current config uses HA mode with Sentinel (3 replicas, masterGroupName). For dev, use a single replica with no Sentinel, matching the local docker-compose setup.

### 4. Remove celery-worker affinity rules

The statefulset has `nodeAffinity` requiring `celery-workhorse=true` labels and hard `podAntiAffinity` requiring each worker on a different node. These will prevent scheduling on a small dev cluster. Remove both for dev.

### 5. Remove celery-worker scratch volume and initContainer

The statefulset references a `job-scratch` volume with an `initContainers` step to fix permissions, but the volume itself isn't defined — it would fail as-is. Not needed for basic crossmatch functionality. Remove the initContainer and volumeMount.

## Open Questions

None — all decisions made during brainstorming.
