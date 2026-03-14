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
        ra_col = catalog_config['ra_column']
        dec_col = catalog_config['dec_column']
        _catalog_cache[name] = lsdb.open_catalog(
            catalog_config['hats_url'],
            columns=[catalog_config['source_id_column'], ra_col, dec_col],
        )
    return _catalog_cache[name]


def crossmatch_alerts(alerts_catalog, catalog_config):
    """Crossmatch an LSDB alerts catalog against a single HATS catalog.

    Args:
        alerts_catalog: Pre-built LSDB catalog from lsdb.from_dataframe().
                        Built once in the task and reused for all catalogs.
        catalog_config: Dict with 'name', 'hats_url', 'source_id_column',
                        'ra_column', 'dec_column'.

    Returns:
        DataFrame with matched rows. Source ID is in the column named by
        catalog_config['source_id_column']. Distance in _dist_arcsec.
        Returns empty DataFrame if no matches found.
    """
    catalog = _get_catalog(catalog_config)
    # Alert DataFrame uses ra_deg/dec_deg; catalog RA/Dec column names vary
    # (e.g. 'ra'/'dec' for Gaia, 'RA'/'DEC' for DES). No overlap with alert
    # columns, so suffix_method='overlapping_columns' won't rename them.
    matches = alerts_catalog.crossmatch(
        catalog,
        n_neighbors=1,
        radius_arcsec=settings.CROSSMATCH_RADIUS_ARCSEC,
        suffixes=('_alert', '_catalog'),
        suffix_method='overlapping_columns',
    )
    return matches.compute()
