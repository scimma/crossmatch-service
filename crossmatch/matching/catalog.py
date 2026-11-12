"""Generic LSDB HATS catalog crossmatch."""

import time

import lsdb
import numpy as np
import pandas as pd
from celery.exceptions import SoftTimeLimitExceeded
from hats.io.skymap import read_skymap
from mocpy import MOC
from django.conf import settings
from core.log import get_logger

logger = get_logger(__name__)

# Class names of transient remote-read failures worth retrying. The HATS
# catalogs for DES/DELVE/SkyMapper are served over HTTP from data.lsdb.io, which
# intermittently drops the connection mid parquet range read. aiohttp raises
# ServerDisconnectedError (and kin); fsspec's parquet cache then re-surfaces it
# as a confusing TypeError ("can't concat ServerDisconnectedError to bytes",
# "'ServerDisconnectedError' object is not subscriptable"). We match on the class
# name so both the raw aiohttp error and its fsspec-wrapped TypeError are caught,
# without importing aiohttp directly.
#
# FileNotFoundError is included deliberately: when data.lsdb.io is slow/flaky
# under a large batch's concurrent range reads, fsspec surfaces the dropped read
# as FileNotFoundError(url) even though the parquet file exists (a direct GET
# returns HTTP 200). Retrying recovers it. A genuinely missing file still fails
# loud after the retries are exhausted, so this only costs a little latency in
# the rare true-missing case; requested-column and catalog-schema errors are
# validated up front in _get_catalog and raise ValueError, not FileNotFoundError.
_TRANSIENT_READ_SIGNATURES = (
    'ServerDisconnectedError',
    'ServerTimeoutError',
    'ClientConnectionError',
    'ClientOSError',
    'ClientPayloadError',
    'ConnectionResetError',
    'FileNotFoundError',
)


def _transient_read_signature(exc: BaseException) -> str | None:
    """The transient-read signature ``exc`` matches, or ``None``.

    Walks the cause/context chain and matches by class name or message text, so
    the raw aiohttp error and fsspec's confusing TypeError-wrapped form (e.g.
    ``TypeError("'ServerDisconnectedError' object is not subscriptable")``) both
    resolve to the underlying transient cause (``ServerDisconnectedError``).
    Returning the signature rather than a bare bool lets callers log the real
    connection failure instead of the misleading wrapper. Deterministic errors
    (bad columns, no spatial overlap, version skew) match nothing.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = f'{type(cur).__name__}: {cur}'
        for sig in _TRANSIENT_READ_SIGNATURES:
            if sig in text:
                return sig
        cur = cur.__cause__ or cur.__context__
    return None


def is_transient_read_error(exc: BaseException) -> bool:
    """True if ``exc`` (or anything in its cause/context chain) is a transient
    remote-read failure worth skipping rather than failing loud.

    The crossmatch loop uses this to decide skip-vs-fail-loud symmetrically with
    the retry gate: only a transient read failure (a flapping catalog host) is
    skippable. Deterministic errors -- a bad/missing/colliding column raised by
    :func:`_get_catalog` (``ValueError``), a dependency/version-skew mismatch, no
    spatial overlap -- return False so they still surface loudly instead of being
    dropped from every future batch.
    """
    return _transient_read_signature(exc) is not None


def _read_with_retry(read_fn, catalog_name: str):
    """Run ``read_fn`` (a catalog read/compute), retrying only on transient
    remote-read disconnects with linear backoff. Non-transient errors re-raise
    immediately, preserving the fail-loud contract in tasks/crossmatch.py.
    """
    attempts = settings.CROSSMATCH_READ_RETRIES
    backoff = settings.CROSSMATCH_READ_RETRY_BACKOFF_SECONDS
    for attempt in range(1, attempts + 1):
        try:
            return read_fn()
        except SoftTimeLimitExceeded:
            # A batch soft time limit is not a retryable read error -- it must
            # propagate so tasks/crossmatch.py reverts the batch. If it fires mid
            # read/compute while a transient disconnect is being handled, Python
            # chains that transient via __context__, which the message-based
            # signature below would match and (wrongly) retry. Match by type first.
            raise
        except Exception as exc:
            signature = _transient_read_signature(exc)
            if attempt < attempts and signature is not None:
                # Name the underlying transient cause (``signature``) so the log
                # is clear even when fsspec wraps it as an opaque TypeError.
                logger.warning(
                    'Transient catalog read error; retrying',
                    catalog=catalog_name,
                    attempt=attempt,
                    max_attempts=attempts,
                    transient=signature,
                    error=str(exc),
                )
                time.sleep(backoff * attempt)
                continue
            raise

# Module-level cache: {catalog_name: lsdb_catalog}
_catalog_cache = {}

# Alert-catalog column names. A requested catalog column matching one of these
# would be renamed with a `_catalog` suffix by the crossmatch
# (suffix_method='overlapping_columns'), which would break payload key mapping
# in the task. None of the current payload columns collide; this guards future
# additions. Keep in sync with the alerts DataFrame built in tasks/crossmatch.py.
_ALERT_COLUMNS = {'uuid', 'lsst_diaObject_diaObjectId', 'ra_deg', 'dec_deg'}


def _load_columns(catalog_config):
    """Ordered, deduplicated list of columns to load for a catalog.

    The source-id / RA / Dec triple plus any configured ``payload_columns``
    (upstream-native case). ``ra``/``dec`` declared in ``payload_columns``
    collapse against ``ra_column``/``dec_column`` here rather than being
    requested twice.
    """
    return list(dict.fromkeys([
        catalog_config['source_id_column'],
        catalog_config['ra_column'],
        catalog_config['dec_column'],
        *catalog_config.get('payload_columns', []),
    ]))


def _get_catalog(catalog_config):
    """Return cached LSDB catalog, loading on first call.

    Loads only the configured columns: the source-id / RA / Dec triple plus the
    catalog's ``payload_columns`` (deduplicated). Requested columns are validated
    against the full catalog schema up front, so a misspelled or wrong-case
    column raises a clear error naming the offender instead of surfacing as a
    cryptic parquet error deep inside ``.compute()`` — which the crossmatch loop
    would otherwise swallow as a generic per-catalog failure.
    """
    name = catalog_config['name']
    if name not in _catalog_cache:
        url = catalog_config['hats_url']
        requested = _load_columns(catalog_config)

        collisions = [c for c in requested if c in _ALERT_COLUMNS]
        if collisions:
            raise ValueError(
                f"{name}: requested columns {collisions} collide with alert "
                f"columns; the crossmatch would suffix them and break payload "
                f"key mapping. Rename or drop them from payload_columns."
            )

        # open_catalog with no `columns` loads only the catalog's *default*
        # columns, so introspect the full schema with columns="all".
        available = set(lsdb.open_catalog(url, columns="all").columns)
        missing = [c for c in requested if c not in available]
        if missing:
            raise ValueError(
                f"{name}: requested columns not found in catalog schema: "
                f"{missing}. Check name/case against docs/references/"
                f"{name}-columns.md."
            )

        logger.info('Loading HATS catalog',
                    catalog=name, url=url, columns=len(requested))
        _catalog_cache[name] = lsdb.open_catalog(url, columns=requested)
    return _catalog_cache[name]


def get_catalog(catalog_config):
    """Return the loaded (cached) LSDB catalog for a configured catalog.

    Public accessor over the module's catalog cache, for callers outside the
    crossmatch path (the replay tool records the catalog's build properties).
    """
    return _get_catalog(catalog_config)


class CatalogCoverageUnavailable(RuntimeError):
    """A catalog in service opened without a HATS coverage map (MOC).

    Without the map the per-object footprint test (KTD6) cannot run, and
    reporting the catalog as searched would overclaim. This is a plan stop
    condition: it must reach the operators, never become a silent fallback.
    """


def catalog_moc(catalog_config):
    """Return the HATS coverage map (a ``mocpy.MOC``) of a configured catalog.

    Reads ``hc_structure.moc`` from the process-cached LSDB catalog, so after the
    catalog's first read this costs no I/O. A catalog built without a coverage
    map (IRSA's Euclid HATS builds) falls back to the populated pixels of its
    HATS skymap, at the skymap's order. Runs in the Celery process, not on the
    Dask cluster.

    Args:
        catalog_config: The catalog's ``CROSSMATCH_CATALOGS`` entry.

    Returns:
        The catalog's coverage map.

    Raises:
        CatalogCoverageUnavailable: If the catalog has neither a coverage map
            nor a readable, non-empty skymap.
    """
    structure = _get_catalog(catalog_config).hc_structure
    moc = structure.moc
    if moc is None:
        moc = _skymap_moc(structure, catalog_config['name'])
    if moc is None:
        raise CatalogCoverageUnavailable(
            f"{catalog_config['name']}: HATS catalog has no coverage map (moc) "
            f"and no usable skymap; per-object footprint outcomes cannot be "
            f"recorded for it"
        )
    return moc


def _skymap_moc(structure, catalog_name: str) -> MOC | None:
    """Build a coverage map from the populated pixels of a HATS skymap.

    Args:
        structure: The catalog's ``hc_structure``.
        catalog_name: The catalog, for the log.

    Returns:
        The coverage map at the skymap's HEALPix order, or ``None`` when the
        skymap cannot be read or has no populated pixel.
    """
    try:
        counts = np.asarray(read_skymap(structure, None))
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 -- reported as coverage unavailable
        logger.warning('Catalog skymap unreadable', catalog=catalog_name,
                       error=repr(exc))
        return None
    pixels = np.nonzero(counts > 0)[0]
    if not len(pixels):
        return None
    order = int(round(np.log(len(counts) / 12) / np.log(4)))
    logger.info('Catalog coverage map built from its skymap',
                catalog=catalog_name, order=order, pixels=len(pixels))
    return MOC.from_healpix_cells(
        pixels.astype(np.uint64), np.full(len(pixels), order, dtype=np.uint8),
        max_depth=order,
    )


def crossmatch_alerts(alerts_catalog, catalog_config):
    """Crossmatch an LSDB alerts catalog against a single HATS catalog.

    Args:
        alerts_catalog: Pre-built LSDB catalog from lsdb.from_dataframe().
                        Built once in the task and reused for all catalogs.
        catalog_config: Dict with 'name', 'hats_url', 'source_id_column',
                        'ra_column', 'dec_column', and optional
                        'payload_columns' (extra columns loaded for the
                        published payload; see _get_catalog).

    Returns:
        DataFrame with matched rows. Source ID is in the column named by
        catalog_config['source_id_column']; the payload columns appear under
        their upstream-native names. Distance in _dist_arcsec. Returns empty
        DataFrame if no matches found.
    """
    # Alert DataFrame uses ra_deg/dec_deg; catalog RA/Dec and payload column
    # names vary (e.g. 'ra'/'dec' for Gaia, 'RA'/'DEC' for DES). The loaded set
    # is now the full payload_columns union, but none overlap the alert columns
    # (_get_catalog rejects any that do via _ALERT_COLUMNS), so
    # suffix_method='overlapping_columns' leaves the catalog columns un-suffixed.
    #
    # The read (catalog open + compute) runs under _read_with_retry so a
    # transient data.lsdb.io disconnect retries instead of failing the whole
    # multi-catalog batch; deterministic errors still fail loud immediately.
    # A 'nearest' catalog keeps the single nearest source in the radius. An
    # 'all' catalog keeps every source in the radius up to max_matches per
    # alert; asking LSDB for one more than the cap is what lets a hit cap be
    # detected and logged rather than silently truncated.
    max_matches = None
    n_neighbors = 1
    if catalog_config.get('match_mode', 'nearest') == 'all':
        max_matches = catalog_config['max_matches']
        n_neighbors = max_matches + 1

    def _read():
        catalog = _get_catalog(catalog_config)
        matches = alerts_catalog.crossmatch(
            catalog,
            n_neighbors=n_neighbors,
            radius_arcsec=settings.CROSSMATCH_RADIUS_ARCSEC,
            suffixes=('_alert', '_catalog'),
            suffix_method='overlapping_columns',
        )
        return matches.compute()

    result = _read_with_retry(_read, catalog_config['name'])
    if max_matches is None:
        return result
    return _cap_matches_per_alert(result, max_matches, catalog_config['name'])


def _cap_matches_per_alert(
    result: pd.DataFrame, max_matches: int, catalog_name: str
) -> pd.DataFrame:
    """Keep at most ``max_matches`` nearest matches per alert.

    Args:
        result: Crossmatch rows with ``lsst_diaObject_diaObjectId`` and
            ``_dist_arcsec`` columns.
        max_matches: The catalog's per-alert cap.
        catalog_name: The catalog, for the truncation log.

    Returns:
        The rows, nearest first within each alert, with any beyond the cap
        dropped. Logs ``crossmatch_match_cap_reached`` once when any alert was
        truncated.
    """
    if result.empty:
        return result
    alert_col = 'lsst_diaObject_diaObjectId'
    counts = result.groupby(alert_col).size()
    truncated = int((counts > max_matches).sum())
    if not truncated:
        return result
    logger.warning(
        'crossmatch_match_cap_reached',
        catalog=catalog_name,
        max_matches=max_matches,
        alerts=truncated,
    )
    ordered = result.sort_values([alert_col, '_dist_arcsec'], kind='stable')
    return ordered.groupby(alert_col, sort=False).head(max_matches)
