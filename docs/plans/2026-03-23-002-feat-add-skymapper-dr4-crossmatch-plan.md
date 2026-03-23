---
title: Add SkyMapper DR4 Crossmatch
type: feat
status: completed
date: 2026-03-23
origin: docs/brainstorms/2026-03-23-add-skymapper-dr4-crossmatch-requirements.md
---

# Add SkyMapper DR4 Crossmatch

## Overview

Add SkyMapper DR4 to the configurable catalog registry. Config-only change following the same pattern as DELVE DR3 Gold. No code changes to matching or task modules.

(See origin: docs/brainstorms/2026-03-23-add-skymapper-dr4-crossmatch-requirements.md)

## Acceptance Criteria

- [x] `SKYMAPPER_HATS_URL` env var added to `settings.py`
- [x] SkyMapper DR4 entry added to `CROSSMATCH_CATALOGS` in `settings.py`
- [x] `SKYMAPPER_HATS_URL` added to celery-worker env in `docker/docker-compose.yaml`
- [x] `SKYMAPPER_HATS_URL` added to `docker/.env.example`
- [x] `SKYMAPPER_HATS_URL` added to `crossmatch.env` in Helm `_helpers.yaml`
- [x] `skymapper_hats_url` added to `crossmatch` section in Helm `values.yaml`
- [x] `skymapper_hats_url` added to `crossmatch` section in `kubernetes/dev-overrides.yaml.example`
- [x] Design document §7.3 updated to list SkyMapper DR4 as active catalog

## Changes

### 1. `crossmatch/project/settings.py`

Add env var (after DELVE_HATS_URL):

```python
SKYMAPPER_HATS_URL = os.getenv('SKYMAPPER_HATS_URL', 'https://data.lsdb.io/hats/skymapper_dr4/catalog')
```

Add to `CROSSMATCH_CATALOGS` list:

```python
    {
        'name': 'skymapper_dr4',
        'hats_url': SKYMAPPER_HATS_URL,
        'source_id_column': 'object_id',
        'ra_column': 'raj2000',
        'dec_column': 'dej2000',
    },
```

### 2. `docker/docker-compose.yaml`

Add to celery-worker environment after `DELVE_HATS_URL`:

```yaml
      SKYMAPPER_HATS_URL: "${SKYMAPPER_HATS_URL:-https://data.lsdb.io/hats/skymapper_dr4/catalog}"
```

### 3. `docker/.env.example`

Add after `DELVE_HATS_URL`:

```
SKYMAPPER_HATS_URL=https://data.lsdb.io/hats/skymapper_dr4/catalog
```

### 4. `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`

Add to `crossmatch.env` block after `DELVE_HATS_URL`:

```yaml
- name: SKYMAPPER_HATS_URL
  value: {{ .Values.crossmatch.skymapper_hats_url | quote }}
```

### 5. `kubernetes/charts/crossmatch-service/values.yaml`

Add to `crossmatch` section after `delve_hats_url`:

```yaml
  skymapper_hats_url: "https://data.lsdb.io/hats/skymapper_dr4/catalog"
```

### 6. `kubernetes/dev-overrides.yaml.example`

Add to `crossmatch` section after `delve_hats_url`:

```yaml
  skymapper_hats_url: "https://data.lsdb.io/hats/skymapper_dr4/catalog"
```

### 7. `scimma_crossmatch_service_design.md`

Update §7.3: add SkyMapper DR4 to active catalogs list, remove from planned list.

## Sources

- **Origin document:** [docs/brainstorms/2026-03-23-add-skymapper-dr4-crossmatch-requirements.md](docs/brainstorms/2026-03-23-add-skymapper-dr4-crossmatch-requirements.md) — Key decisions: column names confirmed (object_id, raj2000, dej2000), config-only change
- **DELVE pattern:** DELVE DR3 Gold addition in same commit (`8fb848b`)
