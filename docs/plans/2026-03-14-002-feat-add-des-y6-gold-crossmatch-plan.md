---
title: "Add DES Y6 Gold catalog crossmatch with configurable catalog list"
type: feat
status: completed
date: 2026-03-14
origin: docs/brainstorms/2026-03-14-add-des-y6-gold-crossmatch-brainstorm.md
---

# Add DES Y6 Gold Catalog Crossmatch

## Overview

Replace the Gaia-only crossmatch with a configurable multi-catalog system.
A catalog registry in Django settings defines which LSDB HATS catalogs to
crossmatch against. The `crossmatch_batch` task loops through all configured
catalogs sequentially. DES Y6 Gold is added as the second catalog alongside
Gaia DR3.

## Proposed Solution

### 1. Add `CROSSMATCH_CATALOGS` setting to `crossmatch/project/settings.py`

Define a list of catalog configurations. Each entry specifies the catalog
name (used in `CatalogMatch.catalog_name`), the LSDB HATS URL (from env
var), and the source ID column name.

```python
# crossmatch/project/settings.py

# Replace the single GAIA_HATS_URL with a catalog registry
DES_HATS_URL = os.getenv('DES_HATS_URL', 'https://data.lsdb.io/hats/des/des_y6_gold')

CROSSMATCH_CATALOGS = [
    {
        'name': 'gaia_dr3',
        'hats_url': GAIA_HATS_URL,
        'source_id_column': 'source_id',
    },
    {
        'name': 'des_y6_gold',
        'hats_url': DES_HATS_URL,
        'source_id_column': 'COADD_OBJECT_ID',
    },
]
```

Keep `GAIA_HATS_URL` and `CROSSMATCH_RADIUS_ARCSEC` as-is — they're still
read from env vars (see brainstorm decision #1).

File: `crossmatch/project/settings.py:10-12` (after existing `GAIA_HATS_URL`)

### 2. Replace `matching/gaia.py` with generic `matching/catalog.py`

Replace the Gaia-specific module with a generic matching function that
accepts catalog config. The module-level cache becomes a dict keyed by
catalog name (see brainstorm decision #6).

```python
# crossmatch/matching/catalog.py

"""Generic LSDB HATS catalog crossmatch."""

import lsdb
import pandas as pd
from django.conf import settings
from core.log import get_logger

logger = get_logger(__name__)

# Module-level cache: {catalog_name: lsdb_catalog}
_catalog_cache = {}


def _get_catalog(catalog_config):
    """Return cached LSDB catalog, loading on first call."""
    name = catalog_config['name']
    if name not in _catalog_cache:
        logger.info('Loading HATS catalog',
                    catalog=name, url=catalog_config['hats_url'])
        _catalog_cache[name] = lsdb.open_catalog(
            catalog_config['hats_url'],
            columns=[catalog_config['source_id_column'], 'ra', 'dec'],
        )
    return _catalog_cache[name]


def crossmatch_alerts(alerts_df: pd.DataFrame,
                      catalog_config: dict) -> pd.DataFrame:
    """Crossmatch alerts against a single LSDB HATS catalog.

    Args:
        alerts_df: DataFrame with columns including ra_deg, dec_deg.
        catalog_config: Dict with 'name', 'hats_url', 'source_id_column'.

    Returns:
        DataFrame with matched rows. Source ID is in the column named by
        catalog_config['source_id_column']. Distance in _dist_arcsec.
    """
    clean_df = alerts_df.dropna(subset=['ra_deg', 'dec_deg'])
    if clean_df.empty:
        logger.warning('No alerts with valid coordinates to crossmatch')
        return pd.DataFrame()

    alerts_catalog = lsdb.from_dataframe(
        clean_df, ra_column='ra_deg', dec_column='dec_deg'
    )
    catalog = _get_catalog(catalog_config)
    matches = alerts_catalog.crossmatch(
        catalog,
        n_neighbors=1,
        radius_arcsec=settings.CROSSMATCH_RADIUS_ARCSEC,
        suffixes=('_alert', '_catalog'),
        suffix_method='overlapping_columns',
    )
    return matches.compute()
```

File: `crossmatch/matching/catalog.py` (new file, replaces `matching/gaia.py`)

### 3. Update `tasks/crossmatch.py` to loop through configured catalogs

Replace the Gaia-hardcoded logic with a loop over `settings.CROSSMATCH_CATALOGS`.
Each catalog produces its own `CatalogMatch` and `Notification` rows
(see brainstorm decisions #3 and #4).

```python
# crossmatch/tasks/crossmatch.py

import pandas as pd
from celery import shared_task
from django.conf import settings
from core.models import Alert, CatalogMatch, Notification
from matching.catalog import crossmatch_alerts
from core.log import get_logger
logger = get_logger(__name__)


@shared_task(name="crossmatch_batch")
def crossmatch_batch(batch_ids: list, match_version: int = 1) -> None:
    """Process a batch of alerts through LSDB crossmatch against all catalogs."""
    if not batch_ids:
        logger.info('No batch IDs provided')
        return

    logger.info('Starting crossmatch batch',
                batch_size=len(batch_ids), match_version=match_version)
    try:
        # 1. Load alerts into DataFrame (once for all catalogs)
        alerts_qs = Alert.objects.filter(pk__in=batch_ids)
        alerts_df = pd.DataFrame(
            alerts_qs.values_list(
                'uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg'
            ),
            columns=['uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg']
        )
        alerts_df['uuid'] = alerts_df['uuid'].astype(str)

        if alerts_df.empty:
            logger.warning('No alerts found for batch IDs',
                           batch_size=len(batch_ids))
            return

        # 2. Crossmatch against each configured catalog sequentially
        for catalog_config in settings.CROSSMATCH_CATALOGS:
            catalog_name = catalog_config['name']
            source_id_col = catalog_config['source_id_column']

            try:
                result_df = crossmatch_alerts(alerts_df, catalog_config)
            except Exception:
                logger.exception('Crossmatch failed for catalog',
                                 catalog=catalog_name)
                continue

            if result_df.empty:
                logger.info('No matches found',
                            catalog=catalog_name, total=len(alerts_df))
                continue

            # 3. Write CatalogMatch rows
            matches_to_create = []
            for _, row in result_df.iterrows():
                matches_to_create.append(CatalogMatch(
                    alert_id=row['lsst_diaObject_diaObjectId'],
                    catalog_name=catalog_name,
                    catalog_source_id=str(row[source_id_col]),
                    match_distance_arcsec=row['_dist_arcsec'],
                    source_ra_deg=row['ra'],
                    source_dec_deg=row['dec'],
                    match_version=match_version,
                ))
            CatalogMatch.objects.bulk_create(
                matches_to_create, ignore_conflicts=True
            )
            logger.info('Wrote CatalogMatch rows',
                        catalog=catalog_name,
                        matched=len(matches_to_create), total=len(alerts_df))

            # 4. Create Notification rows (one per match)
            notifications_to_create = []
            for _, row in result_df.iterrows():
                notifications_to_create.append(Notification(
                    alert_id=row['lsst_diaObject_diaObjectId'],
                    destination='hopskotch',
                    payload={
                        'diaObjectId': int(row['lsst_diaObject_diaObjectId']),
                        'ra': float(row['ra']),
                        'dec': float(row['dec']),
                        'catalog_name': catalog_name,
                        'catalog_source_id': str(row[source_id_col]),
                        'separation_arcsec': float(row['_dist_arcsec']),
                    },
                ))
            Notification.objects.bulk_create(notifications_to_create)
            logger.info('Created Notification rows',
                        catalog=catalog_name,
                        count=len(notifications_to_create))

        # 5. Transition ALL alerts in batch to MATCHED
        Alert.objects.filter(pk__in=batch_ids).update(
            status=Alert.Status.MATCHED
        )
        logger.info('Crossmatch batch complete', batch_size=len(batch_ids))

    except Exception:
        logger.exception('Crossmatch batch failed, reverting to INGESTED',
                         batch_size=len(batch_ids))
        try:
            Alert.objects.filter(pk__in=batch_ids).update(
                status=Alert.Status.INGESTED
            )
        except Exception:
            logger.exception('Failed to revert batch status')
        raise
```

File: `crossmatch/tasks/crossmatch.py` (full rewrite)

### 4. Add `DES_HATS_URL` to Docker Compose and `.env.example`

Add the new env var to `celery-worker` environment in `docker-compose.yaml`
and to `.env.example`.

```yaml
# docker/docker-compose.yaml — celery-worker environment, after GAIA_HATS_URL
      DES_HATS_URL: "${DES_HATS_URL:-https://data.lsdb.io/hats/des/des_y6_gold}"
```

```ini
# docker/.env.example — Crossmatch settings section
DES_HATS_URL=https://data.lsdb.io/hats/des/des_y6_gold
```

Files: `docker/docker-compose.yaml:136-137`, `docker/.env.example:68`

### 5. Delete `matching/gaia.py`

Remove the Gaia-specific module. All references replaced by
`matching/catalog.py` in step 2.

File: `crossmatch/matching/gaia.py` (delete)

### Note: Notification payload format change

The notification payload changes from Gaia-specific (`gaia_source_id`) to
generic (`catalog_name` + `catalog_source_id`). This is a breaking change
to the Hopskotch message format. Already handled in step 3's code.

### Assumptions

- All LSDB HATS catalogs have standard `ra`/`dec` columns. Only the source
  ID column name varies per catalog (see brainstorm decision #6).

## Acceptance Criteria

- [x] `CROSSMATCH_CATALOGS` setting defined with Gaia DR3 and DES Y6 Gold entries
- [x] `DES_HATS_URL` env var added to settings, docker-compose, and .env.example
- [x] `matching/catalog.py` created with generic `crossmatch_alerts()` function
- [x] `matching/gaia.py` deleted
- [x] Catalog cache is a dict keyed by catalog name (not a global singleton)
- [x] `crossmatch_batch` task loops through all `CROSSMATCH_CATALOGS`
- [x] Each catalog match creates its own `CatalogMatch` row with correct `catalog_name`
- [x] Each catalog match creates its own `Notification` row with generic payload
- [x] Notification payload includes `catalog_name` and `catalog_source_id`
- [x] `CROSSMATCH_RADIUS_ARCSEC` applies to all catalogs (global radius)
- [x] Alerts DataFrame loaded once, reused for all catalogs
- [x] Failure in one catalog's crossmatch does not prevent others from running
- [x] All alerts transition to MATCHED after all catalogs are processed

## References

- **Origin brainstorm:** `docs/brainstorms/2026-03-14-add-des-y6-gold-crossmatch-brainstorm.md`
  - Key decisions: configurable catalog list in Django settings, sequential execution
    in same task, generic matching function, one notification per catalog match,
    global match radius, minimal payload format
- Current Gaia matching: `crossmatch/matching/gaia.py`
- Current crossmatch task: `crossmatch/tasks/crossmatch.py`
- Settings: `crossmatch/project/settings.py:10-12`
- CatalogMatch model: `crossmatch/core/models.py:81-114`
- Notification model: `crossmatch/core/models.py:152-188`
- Docker Compose celery-worker: `docker/docker-compose.yaml:89-144`
- DES Y6 Gold LSDB URL: `https://data.lsdb.io/hats/des/des_y6_gold`
