"""Gaia DR3 crossmatch via LSDB."""

import lsdb
import pandas as pd
from django.conf import settings
from core.log import get_logger

logger = get_logger(__name__)

# Module-level cached Gaia catalog (metadata only, lightweight)
_gaia_catalog = None


def _get_gaia_catalog():
    """Return cached Gaia DR3 HATS catalog, loading on first call."""
    global _gaia_catalog
    if _gaia_catalog is None:
        logger.info('Loading Gaia HATS catalog', url=settings.GAIA_HATS_URL)
        _gaia_catalog = lsdb.open_catalog(
            settings.GAIA_HATS_URL,
            columns=['source_id', 'ra', 'dec'],
        )
    return _gaia_catalog


def crossmatch_alerts_against_gaia(alerts_df: pd.DataFrame) -> pd.DataFrame:
    """Crossmatch a DataFrame of alerts against Gaia DR3 via LSDB.

    Args:
        alerts_df: DataFrame with columns including ra_deg, dec_deg.
                   NaN coordinates are filtered before crossmatching.

    Returns:
        DataFrame with matched rows containing merged alert + Gaia columns
        plus _dist_arcsec distance column. Suffixes (_alert, _gaia) applied
        only to overlapping columns (ra, dec).
        Returns empty DataFrame if no alerts have valid coordinates.
    """
    clean_df = alerts_df.dropna(subset=['ra_deg', 'dec_deg'])
    if clean_df.empty:
        logger.warning('No alerts with valid coordinates to crossmatch')
        return pd.DataFrame()

    alerts_catalog = lsdb.from_dataframe(
        clean_df, ra_column='ra_deg', dec_column='dec_deg'
    )
    gaia = _get_gaia_catalog()
    matches = alerts_catalog.crossmatch(
        gaia,
        n_neighbors=1,
        radius_arcsec=settings.CROSSMATCH_RADIUS_ARCSEC,
        suffixes=('_alert', '_gaia'),
        suffix_method='overlapping_columns',
    )
    return matches.compute()
