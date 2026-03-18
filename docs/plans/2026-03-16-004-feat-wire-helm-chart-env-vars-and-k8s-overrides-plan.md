---
title: Wire Helm Chart Env Vars and Create K8s Deployment Overrides
type: feat
status: completed
date: 2026-03-16
origin: docs/brainstorms/2026-03-16-helm-chart-env-gaps-and-k8s-deployment-brainstorm.md
---

# Wire Helm Chart Env Vars and Create K8s Deployment Overrides

## Overview

Add missing env var wiring to the Helm chart so ANTARES, Hopskotch, and crossmatch settings are passed to containers. Create a dev overrides file template and document the deployment steps.

(See brainstorm: docs/brainstorms/2026-03-16-helm-chart-env-gaps-and-k8s-deployment-brainstorm.md)

## Acceptance Criteria

- [x] `_helpers.yaml` has `antares.env`, `hopskotch.env`, and `crossmatch.env` template blocks
- [x] ANTARES credentials use `secretKeyRef` from K8s Secret `antares`
- [x] Hopskotch credentials use `secretKeyRef` from K8s Secret `hopskotch`
- [x] `statefulset.yaml` includes env blocks in the correct containers
- [x] `DES_HATS_URL` added to `crossmatch` section in `values.yaml`
- [x] `kubernetes/dev-overrides.yaml.example` created with all overridable values
- [x] `.gitignore` updated to ignore `kubernetes/dev-overrides.yaml`

## Changes

### 1. `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`

Add three new template blocks:

```yaml
{{- define "antares.env" -}}
- name: ANTARES_API_KEY
  valueFrom:
    secretKeyRef:
      key: ANTARES_API_KEY
      name: antares
- name: ANTARES_API_SECRET
  valueFrom:
    secretKeyRef:
      key: ANTARES_API_SECRET
      name: antares
- name: ANTARES_TOPIC
  value: {{ .Values.antares_consumer.topic | quote }}
- name: ANTARES_GROUP_ID
  value: {{ .Values.antares_consumer.group_id | quote }}
{{- end }}

{{- define "hopskotch.env" -}}
- name: HOPSKOTCH_BROKER_URL
  value: {{ .Values.hopskotch.broker_url | quote }}
- name: HOPSKOTCH_TOPIC
  value: {{ .Values.hopskotch.topic | quote }}
- name: HOPSKOTCH_USERNAME
  valueFrom:
    secretKeyRef:
      key: HOPSKOTCH_USERNAME
      name: hopskotch
- name: HOPSKOTCH_PASSWORD
  valueFrom:
    secretKeyRef:
      key: HOPSKOTCH_PASSWORD
      name: hopskotch
{{- end }}

{{- define "crossmatch.env" -}}
- name: GAIA_HATS_URL
  value: {{ .Values.crossmatch.gaia_hats_url | quote }}
- name: DES_HATS_URL
  value: {{ .Values.crossmatch.des_hats_url | quote }}
- name: CROSSMATCH_RADIUS_ARCSEC
  value: {{ .Values.crossmatch.radius_arcsec | quote }}
- name: CROSSMATCH_BATCH_MAX_WAIT_SECONDS
  value: {{ .Values.crossmatch.batch_max_wait_seconds | quote }}
- name: CROSSMATCH_BATCH_MAX_SIZE
  value: {{ .Values.crossmatch.batch_max_size | quote }}
{{- end }}
```

### 2. `kubernetes/charts/crossmatch-service/templates/statefulset.yaml`

Add include directives to the correct containers:

- **antares-consumer** (after existing includes ~line 36): add `{{- include "antares.env" . | nindent 10 }}`
- **celery-worker** (after existing includes ~line 91): add `{{- include "hopskotch.env" . | nindent 10 }}` and `{{- include "crossmatch.env" . | nindent 10 }}`
- **celery-beat** (after existing includes ~line 175): add `{{- include "crossmatch.env" . | nindent 10 }}`

### 3. `kubernetes/charts/crossmatch-service/values.yaml`

Add `des_hats_url` to the existing `crossmatch` section:

```yaml
crossmatch:
  batch_max_wait_seconds: 900
  batch_max_size: 100000
  gaia_hats_url: "s3://stpubdata/gaia/gaia_dr3/public/hats"
  des_hats_url: "https://data.lsdb.io/hats/des/des_y6_gold"   # new
  radius_arcsec: 1.0
```

### 4. `kubernetes/dev-overrides.yaml.example`

Create a template overrides file showing all overridable values:

```yaml
# Copy to dev-overrides.yaml and fill in values.
# dev-overrides.yaml is gitignored.

antares_consumer:
  topic: "in_lsst_ddf"
  group_id: ""

hopskotch:
  broker_url: "kafka://kafka.scimma.org"
  topic: ""

crossmatch:
  batch_max_wait_seconds: 60
  gaia_hats_url: "s3://stpubdata/gaia/gaia_dr3/public/hats"
  des_hats_url: "https://data.lsdb.io/hats/des/des_y6_gold"
  radius_arcsec: 1.0
```

### 5. `.gitignore`

Add:
```
kubernetes/dev-overrides.yaml
```

## Deployment Steps (for reference)

After chart changes are implemented:

```bash
# 1. Create K8s secrets (all four are required by the chart)
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

# 2. Create overrides file
cp kubernetes/dev-overrides.yaml.example kubernetes/dev-overrides.yaml
# Edit with your values

# 3. Deploy
helm install crossmatch-service kubernetes/charts/crossmatch-service \
  -f kubernetes/dev-overrides.yaml

# 4. Verify
kubectl get pods
kubectl logs <pod-name>
```

## Note: Pre-existing naming mismatch

`values.yaml` defines `antares_consumer:` but the existing templates reference `.Values.alert_consumer` (e.g., `alert_consumer.enabled`, `alert_consumer.app_root_dir`). The new `antares.env` block uses `.Values.antares_consumer.topic` to match `values.yaml`. This naming inconsistency should be resolved separately — either rename the values key to `alert_consumer` or update all template references to `antares_consumer`. For now, be aware that the antares-consumer statefulset guard (`.Values.alert_consumer.enabled`) may not work as expected.

## Sources

- **Origin brainstorm:** [docs/brainstorms/2026-03-16-helm-chart-env-gaps-and-k8s-deployment-brainstorm.md](docs/brainstorms/2026-03-16-helm-chart-env-gaps-and-k8s-deployment-brainstorm.md) — Key decisions: K8s Secrets for credentials, three new env blocks, values already exist except DES_HATS_URL
- **Existing helpers pattern:** `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`
- **Existing secrets pattern:** `_helpers.yaml:15-24` (django secret), `_helpers.yaml:49-53` (database secret)
