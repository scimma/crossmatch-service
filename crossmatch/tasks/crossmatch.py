import lsdb
import pandas as pd
from celery import shared_task
from django.conf import settings
from core.models import Alert, CatalogMatch, Notification
from matching.catalog import crossmatch_alerts
from core.log import get_logger
logger = get_logger(__name__)


@shared_task(name="crossmatch_batch")
def crossmatch_batch(batch_ids: list, match_version: int = 1) -> None:
    """Process a batch of alerts through LSDB crossmatch against all catalogs.

    Args:
        batch_ids: List of alert UUID strings passed from the dispatcher.
        match_version: Schema version for match results.
    """
    if not batch_ids:
        logger.info('No batch IDs provided')
        return

    logger.info('Starting crossmatch batch',
                batch_size=len(batch_ids), match_version=match_version,
                catalogs=len(settings.CROSSMATCH_CATALOGS))
    try:
        # 1. Load alerts into DataFrame (once for all catalogs)
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
            logger.warning('No alerts found for batch IDs',
                           batch_size=len(batch_ids))
            return

        # Build LSDB alerts catalog once, reuse for all reference catalogs
        clean_df = alerts_df.dropna(subset=['ra_deg', 'dec_deg'])
        if clean_df.empty:
            logger.warning('No alerts with valid coordinates to crossmatch')
            Alert.objects.filter(pk__in=batch_ids).update(
                status=Alert.Status.MATCHED
            )
            return
        alerts_catalog = lsdb.from_dataframe(
            clean_df, ra_column='ra_deg', dec_column='dec_deg'
        )

        # 2. Crossmatch against each configured catalog sequentially
        for catalog_config in settings.CROSSMATCH_CATALOGS:
            catalog_name = catalog_config['name']
            source_id_col = catalog_config['source_id_column']
            ra_col = catalog_config['ra_column']
            dec_col = catalog_config['dec_column']

            try:
                result_df = crossmatch_alerts(alerts_catalog, catalog_config)
            except RuntimeError as exc:
                if "Catalogs do not overlap" in str(exc):
                    logger.info('No spatial overlap with catalog',
                                catalog=catalog_name, total=len(clean_df))
                    continue
                raise
            except Exception:
                logger.exception('Crossmatch failed for catalog',
                                 catalog=catalog_name)
                continue

            if result_df.empty:
                logger.info('No matches found',
                            catalog=catalog_name, total=len(clean_df))
                continue

            # Rename _dist_arcsec so itertuples() can access it
            # (namedtuple fields cannot start with underscore)
            result_df = result_df.rename(columns={'_dist_arcsec': 'dist_arcsec'})

            # 3. Build CatalogMatch and Notification rows in single pass
            matches_to_create = []
            notifications_to_create = []
            for row in result_df.itertuples(index=False):
                dia_id = row.lsst_diaObject_diaObjectId
                src_id = str(getattr(row, source_id_col))
                dist = row.dist_arcsec
                ra = getattr(row, ra_col)
                dec = getattr(row, dec_col)

                matches_to_create.append(CatalogMatch(
                    alert_id=dia_id,
                    catalog_name=catalog_name,
                    catalog_source_id=src_id,
                    match_distance_arcsec=dist,
                    source_ra_deg=ra,
                    source_dec_deg=dec,
                    match_version=match_version,
                ))
                notifications_to_create.append(Notification(
                    alert_id=dia_id,
                    destination='hopskotch',
                    payload={
                        'diaObjectId': int(dia_id),
                        'ra': float(ra),
                        'dec': float(dec),
                        'catalog_name': catalog_name,
                        'catalog_source_id': src_id,
                        'separation_arcsec': float(dist),
                    },
                ))

            CatalogMatch.objects.bulk_create(
                matches_to_create, batch_size=5000, ignore_conflicts=True
            )
            Notification.objects.bulk_create(
                notifications_to_create, batch_size=5000
            )
            logger.info('Wrote matches and notifications',
                        catalog=catalog_name,
                        matched=len(matches_to_create), total=len(clean_df))

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
