---
title: Add DELVE DR3 Gold Crossmatch
type: feat
status: completed
date: 2026-03-23
origin: docs/brainstorms/2026-03-23-add-delve-dr3-gold-crossmatch-requirements.md
---

# Add DELVE DR3 Gold Crossmatch

## Overview

Add DELVE DR3 Gold to the configurable catalog registry. Config-only change following the same pattern as the DES Y6 Gold addition. No code changes to matching or task modules.

(See origin: docs/brainstorms/2026-03-23-add-delve-dr3-gold-crossmatch-requirements.md)

## Acceptance Criteria

- [x] `DELVE_HATS_URL` env var added to `settings.py`
- [x] DELVE DR3 Gold entry added to `CROSSMATCH_CATALOGS` in `settings.py`
- [x] `DELVE_HATS_URL` added to celery-worker env in `docker/docker-compose.yaml`
- [x] `DELVE_HATS_URL` added to `docker/.env.example`
- [x] `DELVE_HATS_URL` added to `crossmatch.env` in Helm `_helpers.yaml`
- [x] `delve_hats_url` added to `crossmatch` section in Helm `values.yaml`
- [x] `delve_hats_url` added to `crossmatch` section in `kubernetes/dev-overrides.yaml.example`
- [x] Design document §7.3 updated to list DELVE DR3 Gold as active catalog

## Changes

### 1. `crossmatch/project/settings.py`

Add env var and catalog entry (after DES_HATS_URL, before CROSSMATCH_RADIUS_ARCSEC):

```python
DELVE_HATS_URL = os.getenv('DELVE_HATS_URL', 'https://data.lsdb.io/hats/delve/delve_dr3_gold')
```

Add to `CROSSMATCH_CATALOGS` list:

```python
    {
        'name': 'delve_dr3_gold',
        'hats_url': DELVE_HATS_URL,
        'source_id_column': 'COADD_OBJECT_ID',
        'ra_column': 'RA',
        'dec_column': 'DEC',
    },
```

### 2. `docker/docker-compose.yaml`

Add to celery-worker environment after `DES_HATS_URL`:

```yaml
      DELVE_HATS_URL: "${DELVE_HATS_URL:-https://data.lsdb.io/hats/delve/delve_dr3_gold}"
```

### 3. `docker/.env.example`

Add after `DES_HATS_URL`:

```
DELVE_HATS_URL=https://data.lsdb.io/hats/delve/delve_dr3_gold
```

### 4. `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`

Add to `crossmatch.env` block after `DES_HATS_URL`:

```yaml
- name: DELVE_HATS_URL
  value: {{ .Values.crossmatch.delve_hats_url | quote }}
```

### 5. `kubernetes/charts/crossmatch-service/values.yaml`

Add to `crossmatch` section after `des_hats_url`:

```yaml
  delve_hats_url: "https://data.lsdb.io/hats/delve/delve_dr3_gold"
```

### 6. `kubernetes/dev-overrides.yaml.example`

Add to `crossmatch` section after `des_hats_url`:

```yaml
  delve_hats_url: "https://data.lsdb.io/hats/delve/delve_dr3_gold"
```

### 7. `scimma_crossmatch_service_design.md`

Update §7.3 to move DELVE from planned to active:

- Add DELVE DR3 Gold to the active catalogs list with URL and column details
- Remove DELVE from the planned future catalogs list

## Sources

- **Origin document:** [docs/brainstorms/2026-03-23-add-delve-dr3-gold-crossmatch-requirements.md](docs/brainstorms/2026-03-23-add-delve-dr3-gold-crossmatch-requirements.md) — Key decisions: column names confirmed (COADD_OBJECT_ID, RA, DEC), config-only change, same crossmatch radius
- **DES Y6 Gold pattern:** `crossmatch/project/settings.py:19,30-36`
