# Brainstorm: Fix Helm Chart Env Gaps and Create K8s Deployment Overrides

**Date:** 2026-03-16
**Status:** Draft

## What We're Building

Fix missing environment variables in the Helm chart templates so that all config from the docker-compose `.env` file can be expressed as Helm values, then create an overrides file for deploying to the dev EKS cluster.

## Why This Approach

The Helm chart templates (`_helpers.yaml`) are missing env vars for ANTARES credentials/topic/group_id, Hopskotch settings, and crossmatch settings (GAIA_HATS_URL, DES_HATS_URL, batch thresholds, radius). These are defined in docker-compose.yaml but never got added to the Helm chart. Without them, the K8s deployment can't be fully configured.

## Key Decisions

### 1. Add missing env var blocks to `_helpers.yaml`

The following env vars need to be added as Helm template helpers:

**ANTARES env** (used by antares-consumer):
- `ANTARES_API_KEY` — from K8s Secret (`antares`)
- `ANTARES_API_SECRET` — from K8s Secret (`antares`)
- `ANTARES_TOPIC` — from values
- `ANTARES_GROUP_ID` — from values

**Hopskotch env** (used by celery-worker):
- `HOPSKOTCH_BROKER_URL` — from values
- `HOPSKOTCH_TOPIC` — from values
- `HOPSKOTCH_USERNAME` — from K8s Secret (`hopskotch`)
- `HOPSKOTCH_PASSWORD` — from K8s Secret (`hopskotch`)

**Crossmatch env** (used by celery-worker):
- `GAIA_HATS_URL` — from values
- `DES_HATS_URL` — from values
- `CROSSMATCH_RADIUS_ARCSEC` — from values
- `CROSSMATCH_BATCH_MAX_WAIT_SECONDS` — from values
- `CROSSMATCH_BATCH_MAX_SIZE` — from values

### 2. Use K8s Secrets for credentials

Follow the existing pattern (`database` and `django` secrets). Create two new K8s Secret objects:
- `antares` — keys: `ANTARES_API_KEY`, `ANTARES_API_SECRET`
- `hopskotch` — keys: `HOPSKOTCH_USERNAME`, `HOPSKOTCH_PASSWORD`

These are created with `kubectl create secret` before deploying the Helm chart.

### 3. Include env blocks in the correct statefulset containers

- **antares-consumer**: add `antares.env`
- **celery-worker**: add `hopskotch.env`, `crossmatch.env`
- **celery-beat**: add `crossmatch.env` (for batch thresholds used by dispatch task)

### 4. Values already exist in `values.yaml` — only wiring is missing

The `values.yaml` already has sections for `antares_consumer` (api_key, api_secret, topic, group_id), `hopskotch` (broker_url, topic, username, password), and `crossmatch` (batch_max_wait_seconds, batch_max_size, gaia_hats_url, radius_arcsec) with sensible defaults. The only gap is in `_helpers.yaml` (env var definitions) and `statefulset.yaml` (include directives). `DES_HATS_URL` needs to be added to the `crossmatch` section in `values.yaml` since it's not there yet.

### 5. Create overrides file for dev K8s deployment

Create `kubernetes/dev-overrides.yaml` (gitignored since it may contain non-secret but environment-specific values). The user will fill in exact values for their deployment.

### 6. Deployment walkthrough

After the Helm chart is fixed:
1. Create K8s secrets: `kubectl create secret generic antares ...` and `kubectl create secret generic hopskotch ...`
2. Deploy: `helm install crossmatch-service kubernetes/charts/crossmatch-service -f kubernetes/dev-overrides.yaml`
3. Verify: `kubectl get pods`, check logs

## Open Questions

None — all decisions made during brainstorming.
