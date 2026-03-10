import pandas as pd
from celery import shared_task
from core.models import Alert, CatalogMatch
from matching.gaia import crossmatch_alerts_against_gaia
from core.log import get_logger
logger = get_logger(__name__)


@shared_task(name="crossmatch_batch")
def crossmatch_batch(batch_ids: list, match_version: int = 1) -> None:
    """Process a batch of alerts through LSDB crossmatch against Gaia DR3.

    Args:
        batch_ids: List of alert UUID strings passed from the dispatcher.
        match_version: Schema version for match results.
    """
    if not batch_ids:
        logger.info('No batch IDs provided')
        return

    logger.info('Starting crossmatch batch',
                batch_size=len(batch_ids), match_version=match_version)
    try:
        # 1. Load alerts by batch_ids into DataFrame
        alerts_qs = Alert.objects.filter(pk__in=batch_ids)
        alerts_df = pd.DataFrame(
            alerts_qs.values_list(
                'uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg'
            ),
            columns=['uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg']
        )
        # Convert UUID objects to strings so PyArrow can serialize them
        alerts_df['uuid'] = alerts_df['uuid'].astype(str)

        if alerts_df.empty:
            logger.warning('No alerts found for batch IDs', batch_size=len(batch_ids))
            return

        # 2. Crossmatch via LSDB
        result_df = crossmatch_alerts_against_gaia(alerts_df)

        # 3. Write CatalogMatch rows for matched alerts
        if not result_df.empty:
            matches_to_create = []
            for _, row in result_df.iterrows():
                matches_to_create.append(CatalogMatch(
                    alert_id=row['lsst_diaObject_diaObjectId_alert'],
                    catalog_name='gaia_dr3',
                    catalog_source_id=str(row['source_id_gaia']),
                    match_distance_arcsec=row['_dist_arcsec'],
                    source_ra_deg=row['ra_gaia'],
                    source_dec_deg=row['dec_gaia'],
                    match_version=match_version,
                ))
            CatalogMatch.objects.bulk_create(
                matches_to_create, ignore_conflicts=True
            )
            logger.info('Wrote CatalogMatch rows',
                        matched=len(matches_to_create), total=len(alerts_df))
        else:
            logger.info('No Gaia matches found', total=len(alerts_df))

        # 4. Transition ALL alerts in batch to MATCHED
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
