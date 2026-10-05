import datetime
import logging.config
import os

from django.core.exceptions import ImproperlyConfigured


######################################################################
# Application config
#
APP_VERSION = os.getenv('APP_VERSION', '0.0.0')

# Dask distributed scheduler (optional)
# When set, Celery workers connect to a remote Dask scheduler.
# When empty, Dask uses its default local synchronous scheduler.
# In K8s, set from HOPDEVEL_DASK_SCHEDULER_SERVICE_HOST and
# HOPDEVEL_DASK_SCHEDULER_SERVICE_PORT_TCP_COMM.
DASK_SCHEDULER_ADDRESS = os.getenv('DASK_SCHEDULER_ADDRESS', '')

# Maximum seconds to wait for the Dask cluster to be reachable AND for at
# least one worker to register before failing the version-drift check at
# Celery worker startup. See crossmatch/core/dask.py.
DASK_VERSION_CHECK_TIMEOUT_SECONDS = int(os.getenv('DASK_VERSION_CHECK_TIMEOUT_SECONDS', '300'))

# LSDB crossmatch settings
GAIA_HATS_URL = os.getenv('GAIA_HATS_URL', 's3://stpubdata/gaia/gaia_dr3/public/hats')
DES_HATS_URL = os.getenv('DES_HATS_URL', 's3://stpubdata/mast/public/des/hats/des_y6_gold')
DELVE_HATS_URL = os.getenv('DELVE_HATS_URL', 's3://stpubdata/mast/public/delve/hats/delve_dr3_gold')
SKYMAPPER_HATS_URL = os.getenv('SKYMAPPER_HATS_URL', 'https://data.lsdb.io/hats/skymapper_dr4/catalog')
# Catalog release labels reported as provenance (KTD7). Configured labels, not
# read from the HATS build, so the web tier and API never open LSDB to report
# them. Change a label together with its HATS URL when a new release is served.
GAIA_RELEASE = os.getenv('GAIA_RELEASE', 'Gaia DR3')
DES_RELEASE = os.getenv('DES_RELEASE', 'DES Y6 Gold')
DELVE_RELEASE = os.getenv('DELVE_RELEASE', 'DELVE DR3 Gold')
SKYMAPPER_RELEASE = os.getenv('SKYMAPPER_RELEASE', 'SkyMapper DR4')


def _env_moc_order(name, default):
    """Read an optional HEALPix order from the environment.

    Args:
        name: The environment variable.
        default: The order when the variable is unset.

    Returns:
        The order as an int, ``default`` when unset, or ``None`` when set to an
        empty string (not configured).

    Raises:
        ImproperlyConfigured: If the value is not an integer.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.strip() == '':
        return None
    try:
        return int(raw)
    except ValueError:
        raise ImproperlyConfigured(
            f'{name} must be an integer HEALPix order 0..29 (got {raw!r})'
        ) from None


# Max HEALPix order of each served catalog's HATS coverage map
# (hc_structure.moc), reported by api/describe so users know the resolution at
# which a catalog outcome of "searched" was decided: the crossmatch footprint
# test uses that coverage map, so an object in a footprint hole or near an edge
# can be recorded as searched. Configured, like the release labels, so the web
# tier and API never open LSDB/HATS to report it; change it together with the
# HATS URL. Order 8 is about 14 arcmin pixels, 6 about 55, 10 about 3.4. An
# empty value reports the order as not configured.
GAIA_FOOTPRINT_MOC_ORDER = _env_moc_order('GAIA_FOOTPRINT_MOC_ORDER', 8)
DES_FOOTPRINT_MOC_ORDER = _env_moc_order('DES_FOOTPRINT_MOC_ORDER', 6)
DELVE_FOOTPRINT_MOC_ORDER = _env_moc_order('DELVE_FOOTPRINT_MOC_ORDER', 10)
SKYMAPPER_FOOTPRINT_MOC_ORDER = _env_moc_order('SKYMAPPER_FOOTPRINT_MOC_ORDER', 8)
CROSSMATCH_RADIUS_ARCSEC = float(os.getenv('CROSSMATCH_RADIUS_ARCSEC', '1.0'))

CROSSMATCH_CATALOGS = [
    {
        'name': 'gaia_dr3',
        'hats_url': GAIA_HATS_URL,
        'release': GAIA_RELEASE,
        'footprint_moc_order': GAIA_FOOTPRINT_MOC_ORDER,
        'source_id_column': 'source_id',
        'ra_column': 'ra',
        'dec_column': 'dec',
        # Core payload columns (upstream-native case; Gaia is lowercase).
        # Lowercasing for the published payload happens at build time.
        'payload_columns': [
            # brightness
            'phot_g_mean_mag', 'phot_bp_mean_mag', 'phot_rp_mean_mag',
            'phot_g_mean_flux_over_error', 'phot_bp_mean_flux_over_error',
            'phot_rp_mean_flux_over_error',
            # location
            'ra', 'dec', 'ra_error', 'dec_error', 'parallax', 'parallax_error',
            'pmra', 'pmra_error', 'pmdec', 'pmdec_error', 'ref_epoch',
            # classification
            'classprob_dsc_combmod_star', 'classprob_dsc_combmod_galaxy',
            'classprob_dsc_combmod_quasar',
            # quality
            'ruwe', 'astrometric_excess_noise', 'astrometric_excess_noise_sig',
        ],
        # Key values shown per coincident source by the chat connector
        # (KTD10): a subset of payload_columns (upstream-native case), served
        # under the lowercased payload keys.
        'key_columns': [
            'phot_g_mean_mag', 'parallax', 'parallax_error',
            'classprob_dsc_combmod_star', 'classprob_dsc_combmod_galaxy',
        ],
        # Columns the API can filter on (R13; KTD9): a subset of payload_columns
        # (upstream-native case) with their units, served as
        # gaia_dr3.<column>_min/_max over the stored (lowercased) payload keys.
        'filter_columns': {
            'parallax': 'mas',
            'parallax_error': 'mas',
            'pmra': 'mas/yr',
            'pmdec': 'mas/yr',
            'ruwe': 'dimensionless',
            'classprob_dsc_combmod_star': 'probability',
            'classprob_dsc_combmod_galaxy': 'probability',
            'classprob_dsc_combmod_quasar': 'probability',
        },
        # Filterable values computed from stored payload values as
        # numerator / denominator (signed; null when either is missing or the
        # denominator is 0). Gaia's own parallax_over_error is not stored.
        'derived_filter_columns': {
            'parallax_over_error': {
                'numerator': 'parallax',
                'denominator': 'parallax_error',
                'unit': 'dimensionless',
            },
        },
    },
    {
        'name': 'des_y6_gold',
        'hats_url': DES_HATS_URL,
        'release': DES_RELEASE,
        'footprint_moc_order': DES_FOOTPRINT_MOC_ORDER,
        'source_id_column': 'COADD_OBJECT_ID',
        'ra_column': 'RA',
        'dec_column': 'DEC',
        # Core payload columns (upstream-native case; DES is UPPERCASE).
        # RA/DEC use the UPPERCASE form so the loader dedups them against
        # ra_column/dec_column instead of requesting a non-existent column.
        'payload_columns': [
            # brightness (5 bands: g r i z Y)
            'WAVG_MAG_PSF_G', 'WAVG_MAG_PSF_R', 'WAVG_MAG_PSF_I',
            'WAVG_MAG_PSF_Z', 'WAVG_MAG_PSF_Y',
            'WAVG_MAGERR_PSF_G', 'WAVG_MAGERR_PSF_R', 'WAVG_MAGERR_PSF_I',
            'WAVG_MAGERR_PSF_Z', 'WAVG_MAGERR_PSF_Y',
            # location
            'RA', 'DEC',
            # shape
            'BDF_T', 'BDF_G_1', 'BDF_G_2', 'BDF_FRACDEV',
            # photo-z
            'DNF_Z', 'DNF_ZSIGMA',
            # classification
            'EXT_MASH',
            # quality
            'FLAGS_GOLD', 'FLAGS_FOREGROUND', 'FLAGS_FOOTPRINT', 'BDF_FLAGS',
        ],
        # Key values for the chat connector (see gaia_dr3).
        'key_columns': [
            'WAVG_MAG_PSF_G', 'WAVG_MAG_PSF_R', 'WAVG_MAG_PSF_I', 'EXT_MASH', 'DNF_Z',
        ],
        # Filterable columns with units (see gaia_dr3).
        'filter_columns': {
            'DNF_Z': 'dimensionless',
            'DNF_ZSIGMA': 'dimensionless',
            'EXT_MASH': 'class code (0-4)',
        },
    },
    {
        'name': 'delve_dr3_gold',
        'hats_url': DELVE_HATS_URL,
        'release': DELVE_RELEASE,
        'footprint_moc_order': DELVE_FOOTPRINT_MOC_ORDER,
        'source_id_column': 'COADD_OBJECT_ID',
        'ra_column': 'RA',
        'dec_column': 'DEC',
        # Core payload columns (upstream-native case; DELVE is UPPERCASE).
        # Same as DES Y6 Gold minus the Y band (DELVE has g r i z only).
        'payload_columns': [
            # brightness (4 bands: g r i z)
            'WAVG_MAG_PSF_G', 'WAVG_MAG_PSF_R', 'WAVG_MAG_PSF_I',
            'WAVG_MAG_PSF_Z',
            'WAVG_MAGERR_PSF_G', 'WAVG_MAGERR_PSF_R', 'WAVG_MAGERR_PSF_I',
            'WAVG_MAGERR_PSF_Z',
            # location
            'RA', 'DEC',
            # shape
            'BDF_T', 'BDF_G_1', 'BDF_G_2', 'BDF_FRACDEV',
            # photo-z
            'DNF_Z', 'DNF_ZSIGMA',
            # classification
            'EXT_MASH',
            # quality
            'FLAGS_GOLD', 'FLAGS_FOREGROUND', 'FLAGS_FOOTPRINT', 'BDF_FLAGS',
        ],
        # Key values for the chat connector (see gaia_dr3).
        'key_columns': [
            'WAVG_MAG_PSF_G', 'WAVG_MAG_PSF_R', 'WAVG_MAG_PSF_I', 'EXT_MASH', 'DNF_Z',
        ],
        # Filterable columns with units (see gaia_dr3).
        'filter_columns': {
            'DNF_Z': 'dimensionless',
            'DNF_ZSIGMA': 'dimensionless',
            'EXT_MASH': 'class code (0-4)',
        },
    },
    {
        'name': 'skymapper_dr4',
        'hats_url': SKYMAPPER_HATS_URL,
        'release': SKYMAPPER_RELEASE,
        'footprint_moc_order': SKYMAPPER_FOOTPRINT_MOC_ORDER,
        'source_id_column': 'object_id',
        'ra_column': 'raj2000',
        'dec_column': 'dej2000',
        # Core payload columns (upstream-native case; SkyMapper is lowercase
        # with J2000 coordinate suffix, preserved in the payload). Only PSF
        # photometry exists here; no shape or photo-z columns.
        'payload_columns': [
            # brightness (6 bands: u v g r i z)
            'u_psf', 'v_psf', 'g_psf', 'r_psf', 'i_psf', 'z_psf',
            'e_u_psf', 'e_v_psf', 'e_g_psf', 'e_r_psf', 'e_i_psf', 'e_z_psf',
            # location
            'raj2000', 'dej2000', 'e_raj2000', 'e_dej2000',
            # classification
            'class_star',
            # quality
            'flags', 'nimaflags', 'ngood',
        ],
        # Key values for the chat connector (see gaia_dr3).
        'key_columns': ['g_psf', 'r_psf', 'class_star'],
        # No filterable columns (R13 names none for SkyMapper).
        'filter_columns': {},
    },
]


def _validate_catalog_releases(catalogs):
    """Require a non-empty ``release`` label on every catalog entry (KTD7).

    Args:
        catalogs: The ``CROSSMATCH_CATALOGS`` list.

    Raises:
        ImproperlyConfigured: If an entry has no ``release`` or a blank one.
    """
    for cat in catalogs:
        release = cat.get('release')
        if not isinstance(release, str) or not release.strip():
            raise ImproperlyConfigured(
                f"CROSSMATCH_CATALOGS entry {cat.get('name')!r} needs a non-empty "
                f"'release' label (got {release!r})"
            )


_validate_catalog_releases(CROSSMATCH_CATALOGS)


def _validate_footprint_moc_orders(catalogs):
    """Require any configured ``footprint_moc_order`` to be a HEALPix order.

    The key is optional; ``None`` means not configured.

    Args:
        catalogs: The ``CROSSMATCH_CATALOGS`` list.

    Raises:
        ImproperlyConfigured: If a configured order is not an int in 0..29.
    """
    for cat in catalogs:
        order = cat.get('footprint_moc_order')
        if order is None:
            continue
        if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= 29:
            raise ImproperlyConfigured(
                f"CROSSMATCH_CATALOGS entry {cat.get('name')!r}: "
                f"footprint_moc_order must be an int HEALPix order 0..29 "
                f"(got {order!r})"
            )


_validate_footprint_moc_orders(CROSSMATCH_CATALOGS)


def _validate_filter_columns(catalogs):
    """Require every filterable column to be a payload column with a unit (KTD9).

    ``filter_columns`` maps upstream-native column names (exact case) to units;
    ``derived_filter_columns`` maps a name to ``numerator``/``denominator``
    payload columns and a unit. Public filter names are lowercased, so no two
    names of one catalog may collide once lowercased.

    Args:
        catalogs: The ``CROSSMATCH_CATALOGS`` list.

    Raises:
        ImproperlyConfigured: If a filter column (or a derived filter's operand)
            is not in the catalog's ``payload_columns``, a unit is missing or
            blank, or two filter names collide.
    """
    for cat in catalogs:
        name = cat.get('name')
        payload = set(cat.get('payload_columns') or [])
        seen = set()

        def _check_name(filter_name):
            if filter_name.lower() in seen:
                raise ImproperlyConfigured(
                    f"CROSSMATCH_CATALOGS entry {name!r}: filter name "
                    f"{filter_name!r} collides with another once lowercased"
                )
            seen.add(filter_name.lower())

        def _check_unit(filter_name, unit):
            if not isinstance(unit, str) or not unit.strip():
                raise ImproperlyConfigured(
                    f"CROSSMATCH_CATALOGS entry {name!r}: filter {filter_name!r} "
                    f"needs a non-empty unit (got {unit!r})"
                )

        for column, unit in (cat.get('filter_columns') or {}).items():
            if column not in payload:
                raise ImproperlyConfigured(
                    f"CROSSMATCH_CATALOGS entry {name!r}: filter column {column!r} "
                    f"is not in payload_columns (names are case-sensitive)"
                )
            _check_unit(column, unit)
            _check_name(column)
        for derived, spec in (cat.get('derived_filter_columns') or {}).items():
            for operand in ('numerator', 'denominator'):
                column = (spec or {}).get(operand)
                if column not in payload:
                    raise ImproperlyConfigured(
                        f"CROSSMATCH_CATALOGS entry {name!r}: derived filter "
                        f"{derived!r} {operand} {column!r} is not in payload_columns"
                    )
            _check_unit(derived, spec.get('unit'))
            _check_name(derived)


_validate_filter_columns(CROSSMATCH_CATALOGS)


def _validate_key_columns(catalogs):
    """Require every chat key column to be a payload column (KTD10).

    ``key_columns`` is optional; when present it lists upstream-native column
    names (exact case), shown by the chat connector under their lowercased
    payload keys.

    Args:
        catalogs: The ``CROSSMATCH_CATALOGS`` list.

    Raises:
        ImproperlyConfigured: If a key column is not in the catalog's
            ``payload_columns``.
    """
    for cat in catalogs:
        payload = set(cat.get('payload_columns') or [])
        for column in cat.get('key_columns') or []:
            if column not in payload:
                raise ImproperlyConfigured(
                    f"CROSSMATCH_CATALOGS entry {cat.get('name')!r}: key column "
                    f"{column!r} is not in payload_columns (names are case-sensitive)"
                )


_validate_key_columns(CROSSMATCH_CATALOGS)

# Batch crossmatch thresholds
CROSSMATCH_BATCH_MAX_WAIT_SECONDS = int(
    os.getenv('CROSSMATCH_BATCH_MAX_WAIT_SECONDS', '900')
)
CROSSMATCH_BATCH_MAX_SIZE = int(
    os.getenv('CROSSMATCH_BATCH_MAX_SIZE', '100000')
)
# crossmatch_batch runtime bounds. The SOFT limit self-heals an overrunning live
# batch: it raises SoftTimeLimitExceeded, which the task's on-raise path reverts to
# INGESTED for re-dispatch. The hard TIME limit is the SIGKILL backstop for a Dask
# call that never returns to Python for the soft signal. Sized from the measured
# 100k-batch worst case (~4.3 min). See the ordering constraint on
# CROSSMATCH_BATCH_STUCK_SECONDS below and
# docs/plans/2026-07-20-001-fix-crossmatch-batch-kill-recovery-plan.md.
CROSSMATCH_BATCH_SOFT_TIME_LIMIT_SECONDS = int(
    os.getenv('CROSSMATCH_BATCH_SOFT_TIME_LIMIT_SECONDS', '480')
)
CROSSMATCH_BATCH_TIME_LIMIT_SECONDS = int(
    os.getenv('CROSSMATCH_BATCH_TIME_LIMIT_SECONDS', '600')
)
# A QUEUED batch whose worker was hard-killed (pod restart, OOM, SIGKILL) never
# runs the crossmatch task's own revert-on-exception path, so the dispatcher
# recovers it: QUEUED alerts whose queued_at is older than this are reverted to
# INGESTED and re-dispatched. Measured against queued_at (when the batch was
# dispatched), NOT ingest_time, so it never reverts a live batch of
# long-ingested alerts.
#
# Ordering constraint (so the timer never reclaims a LIVE batch):
#   CROSSMATCH_BATCH_SOFT_TIME_LIMIT_SECONDS
#     < CROSSMATCH_BATCH_TIME_LIMIT_SECONDS
#     < CROSSMATCH_BATCH_STUCK_SECONDS,
# and this must also exceed the soft limit PLUS worst-case broker/pickup latency
# PLUS a clock-skew allowance. A live batch that overruns its soft limit
# self-reverts before this timer can fire; this timer only catches the hard-kill
# case. Defaults: soft 480s < hard 600s < this 780s (~5 min margin over soft)
# -> recovery ~13 min. (Tune all three together; test_time_limits_below_stuck_
# threshold pins the shipped defaults.)
CROSSMATCH_BATCH_STUCK_SECONDS = int(
    os.getenv('CROSSMATCH_BATCH_STUCK_SECONDS', '780')
)

######################################################################
# Payload retention (null-in-place)
#
# The retention sweep nulls raw payloads for terminal alerts/notifications older
# than the grace period, keeping the rows (the result lives in catalog_matches /
# core_notification). PROD uses the 30-day default; DEV overrides to a short window
# via env (its ~56 KB payloads fill any 30-day window long before 30 days). The
# per-run row cap keeps the sweep from starving ingest/crossmatch/notify.
CROSSMATCH_RETENTION_GRACE_DAYS = int(
    os.getenv('CROSSMATCH_RETENTION_GRACE_DAYS', '30')
)
CROSSMATCH_RETENTION_MAX_ROWS = int(
    os.getenv('CROSSMATCH_RETENTION_MAX_ROWS', '10000')
)
CROSSMATCH_RETENTION_INTERVAL_SECONDS = int(
    os.getenv('CROSSMATCH_RETENTION_INTERVAL_SECONDS', '3600')
)

# Fail fast on a misconfigured deploy rather than silently letting the recovery
# timer reclaim a live batch (which would re-dispatch a concurrent run). All three
# bounds are independently env-configurable, so enforce the ordering at startup.
if not (
    CROSSMATCH_BATCH_SOFT_TIME_LIMIT_SECONDS
    < CROSSMATCH_BATCH_TIME_LIMIT_SECONDS
    < CROSSMATCH_BATCH_STUCK_SECONDS
):
    raise ImproperlyConfigured(
        'Crossmatch batch time bounds must satisfy '
        'CROSSMATCH_BATCH_SOFT_TIME_LIMIT_SECONDS < '
        'CROSSMATCH_BATCH_TIME_LIMIT_SECONDS < CROSSMATCH_BATCH_STUCK_SECONDS so the '
        'stuck-batch recovery timer never reclaims a live batch (got '
        f'{CROSSMATCH_BATCH_SOFT_TIME_LIMIT_SECONDS} / '
        f'{CROSSMATCH_BATCH_TIME_LIMIT_SECONDS} / '
        f'{CROSSMATCH_BATCH_STUCK_SECONDS}).'
    )

# Resilience for transient remote HATS catalog reads (data.lsdb.io drops
# connections mid parquet read -> aiohttp ServerDisconnectedError). Total read
# attempts per catalog and the linear backoff base between them. Set retries to
# 1 to disable retrying.
CROSSMATCH_READ_RETRIES = int(os.getenv('CROSSMATCH_READ_RETRIES', '3'))
CROSSMATCH_READ_RETRY_BACKOFF_SECONDS = float(
    os.getenv('CROSSMATCH_READ_RETRY_BACKOFF_SECONDS', '1.0')
)

# Crossmatch replay (docs/runbooks/crossmatch-replay.md): replay_run refuses
# unless this is set. Enable it on DEV only -- replays must never run against the
# PROD Dask cluster.
CROSSMATCH_REPLAY_ENABLED = os.getenv(
    'CROSSMATCH_REPLAY_ENABLED', 'false'
).lower() in ('true', '1', 'yes')

######################################################################
# Django apps and middlewares
#
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_celery_results',
    'django_celery_beat',
    'project',
    'core',
    'tasks',
    'api',
    'web',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Serve the web frontend's collected static (logo, css) straight from the
    # gunicorn pod (no nginx). Must sit directly after SecurityMiddleware so it
    # sees requests before CommonMiddleware. See STORAGES / WHITENOISE_USE_FINDERS
    # below and entrypoints/run_web.sh (collectstatic).
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.common.CommonMiddleware',
]

######################################################################
# Generic application config
#
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-dummy-secret')
DJANGO_SUPERUSER_USERNAME = os.getenv('DJANGO_SUPERUSER_USERNAME', 'admin')
APP_ROOT_DIR = os.environ.get('APP_ROOT_DIR', '/opt')
assert os.path.isabs(APP_ROOT_DIR)
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SITE_ID = 1
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_L10N = False
USE_TZ = True
DATETIME_FORMAT = 'Y-m-d H:m:s'
DATE_FORMAT = 'Y-m-d'
# Caching
VALKEY_SERVICE = os.environ.get('VALKEY_SERVICE', 'redis')
VALKEY_PORT = int(os.environ.get('VALKEY_PORT', '6379'))
# If running Redis in high-availability mode using Sentinel, there must be a master group name set
VALKEY_MASTER_GROUP_NAME = os.environ.get('VALKEY_MASTER_GROUP_NAME', '')
VALKEY_OR_SENTINEL = 'sentinel' if VALKEY_MASTER_GROUP_NAME else 'redis'
# Caching config
CACHES = {
    'default': {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"{VALKEY_OR_SENTINEL}://{VALKEY_SERVICE}:{VALKEY_PORT}",
    }
}

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

######################################################################
# Celery config
#
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TIMEZONE = "UTC"
CELERY_IMPORTS = [
    "tasks.crossmatch",
    "tasks.schedule",
]
CELERY_TASK_ROUTES = {}
CELERY_TASK_DEFAULT_QUEUE = 'alerts'
# Backends & brokers
CELERY_BROKER_URL = f"{VALKEY_OR_SENTINEL}://{VALKEY_SERVICE}:{VALKEY_PORT}"
CELERY_BROKER_TRANSPORT_OPTIONS = {'master_name': VALKEY_MASTER_GROUP_NAME}
# Results backend
CELERY_RESULT_BACKEND = f"{VALKEY_OR_SENTINEL}://{VALKEY_SERVICE}:{VALKEY_PORT}"
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {
    'master_name': VALKEY_MASTER_GROUP_NAME,
    'retry_policy': {
        'timeout': 5.0
    }
}
CELERYD_REDIRECT_STDOUTS_LEVEL = "INFO"
CELERY_TASK_SOFT_TIME_LIMIT = int(os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "3600"))
CELERY_TASK_TIME_LIMIT = int(os.environ.get("CELERY_TASK_TIME_LIMIT", "3800"))
CELERY_TASK_TRACK_STARTED = True
# Emit task lifecycle events so grafana/celery-exporter reports per-task
# success/failure/runtime series, not just broker queue length (U7). Declarative
# config is preferred over passing -E on the worker command line.
# task_send_sent_event adds the task-sent event (publish-time / queue latency).
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True

######################################################################
# Lasair Kafka consumer
#
_lasair_group_id = os.environ.get('LASAIR_GROUP_ID', '')
if not _lasair_group_id:
    import time as _time
    _lasair_group_id = f'scimma-crossmatch-dev-{int(_time.time())}'
LASAIR_KAFKA_SERVER = os.environ.get('LASAIR_KAFKA_SERVER', 'lasair-lsst-kafka.lsst.ac.uk:9092')
LASAIR_TOPIC = os.environ.get('LASAIR_TOPIC', 'lasair_366SCiMMA_reliability_moderate')
LASAIR_GROUP_ID = _lasair_group_id

######################################################################
# Broker filter standard — see scimma_crossmatch_service_design.md §2.2
#
# Single rule applied across every broker (ANTARES, Lasair, Pitt-Google):
# the latest diaSource for a diaObject must have reliability >= this
# threshold. Reliability is the LSST DM real/bogus score (RBTransiNetTask,
# DM-39378). Broker-agnostic so future broker clients added under
# crossmatch/brokers/<broker>/ consume the same variable.
#
# Bounds-checked at import: a non-numeric value raises ValueError;
# anything outside [0.0, 1.0] (including nan, inf, negative, > 1) raises
# ImproperlyConfigured. The chained inequality below also rejects nan
# because nan comparisons return False.
_min_reliability = float(os.environ.get('MIN_DIASOURCE_RELIABILITY', '0.6'))
if not (0.0 <= _min_reliability <= 1.0):
    raise ImproperlyConfigured(
        f'MIN_DIASOURCE_RELIABILITY must be a finite float in [0.0, 1.0]; '
        f'got {_min_reliability!r}'
    )
MIN_DIASOURCE_RELIABILITY = _min_reliability

######################################################################
# ANTARES streaming consumer
#
ANTARES_API_KEY = os.environ.get('ANTARES_API_KEY', '')
ANTARES_API_SECRET = os.environ.get('ANTARES_API_SECRET', '')
ANTARES_TOPIC = os.environ.get('ANTARES_TOPIC', 'lsst_scimma_quality_transient')
_antares_group_id = os.environ.get('ANTARES_GROUP_ID', '')
if not _antares_group_id:
    import time as _time
    _antares_group_id = f'scimma-crossmatch-dev-{int(_time.time())}'
ANTARES_GROUP_ID = _antares_group_id

######################################################################
# Pitt-Google Pub/Sub consumer
#
PITTGOOGLE_TOPIC = os.environ.get('PITTGOOGLE_TOPIC', 'lsst-alerts-json')
PITTGOOGLE_SUBSCRIPTION = os.environ.get('PITTGOOGLE_SUBSCRIPTION', 'scimma-crossmatch-lsst-alerts-json')
PITTGOOGLE_PUBLISHER_PROJECT = os.environ.get('PITTGOOGLE_PUBLISHER_PROJECT', 'pitt-alert-broker')
# GCP auth is handled by standard env vars:
#   GOOGLE_CLOUD_PROJECT — the subscriber's GCP project (where the subscription lives)
#   GOOGLE_APPLICATION_CREDENTIALS — path to service account JSON key file

######################################################################
# SCiMMA Hopskotch publisher
#
HOPSKOTCH_BROKER_URL = os.environ.get('HOPSKOTCH_BROKER_URL', 'kafka://kafka.scimma.org')
HOPSKOTCH_TOPIC = os.environ.get('HOPSKOTCH_TOPIC', '')
HOPSKOTCH_USERNAME = os.environ.get('HOPSKOTCH_USERNAME', '')
HOPSKOTCH_PASSWORD = os.environ.get('HOPSKOTCH_PASSWORD', '')

######################################################################
# TNS (Transient Name Service) cross-link
#
# A local snapshot of TNS's public object list is refreshed by the
# refresh_tns_snapshot Beat task and positionally associated against each alert
# at TNS_MATCH_RADIUS_ARCSEC — its own knob, independent of the catalog radius,
# since it is tuned for transient identity, not catalog sources.
TNS_MATCH_RADIUS_ARCSEC = float(os.getenv('TNS_MATCH_RADIUS_ARCSEC', '1.0'))
if not (TNS_MATCH_RADIUS_ARCSEC > 0):
    raise ImproperlyConfigured(
        'TNS_MATCH_RADIUS_ARCSEC must be a positive number of arcseconds '
        f'(got {TNS_MATCH_RADIUS_ARCSEC}).'
    )
# How often the snapshot-refresh Beat task runs.
TNS_SNAPSHOT_REFRESH_INTERVAL_SECONDS = int(
    os.getenv('TNS_SNAPSHOT_REFRESH_INTERVAL_SECONDS', '3600')
)
# A snapshot whose epoch is older than this is treated as stale/not-current: the
# association marks alerts checked=False and emits no tns block. Default ~2x the
# refresh interval so a single missed refresh does not immediately go stale.
TNS_SNAPSHOT_MAX_AGE_SECONDS = int(
    os.getenv('TNS_SNAPSHOT_MAX_AGE_SECONDS', '7200')
)
# TNS bot credentials for the authenticated bulk download (a tns_marker
# User-Agent + api_key form field, one call per refresh). Empty by default; a
# real refresh needs them provisioned as a per-cluster sealed secret on the
# celery-worker / celery-beat workloads only (never the web pod).
TNS_BOT_ID = os.environ.get('TNS_BOT_ID', '')
TNS_BOT_NAME = os.environ.get('TNS_BOT_NAME', '')
TNS_BOT_API_KEY = os.environ.get('TNS_BOT_API_KEY', '')
# Base URL for the TNS public-objects bulk exports (full file + hourly deltas).
TNS_OBJECTS_BASE_URL = os.getenv(
    'TNS_OBJECTS_BASE_URL',
    'https://www.wis-tns.org/system/files/tns_public_objects/',
)
# Template for a TNS object page; {name} is the bare designation (no AT/SN prefix).
TNS_OBJECT_URL_TEMPLATE = os.getenv(
    'TNS_OBJECT_URL_TEMPLATE', 'https://www.wis-tns.org/object/{name}'
)
# Broker object links shown by the chat connector (KTD11); {diaObjectId} is the
# decimal diaObjectId. ANTARES keys objects by its own locus ID, which this
# service does not store, so its link is a search. Both forms are best current
# knowledge and must be confirmed against the live Lasair and ANTARES sites
# before launch.
LASAIR_OBJECT_URL_TEMPLATE = os.getenv(
    'LASAIR_OBJECT_URL_TEMPLATE', 'https://lasair.lsst.ac.uk/objects/{diaObjectId}/'
)
ANTARES_OBJECT_URL_TEMPLATE = os.getenv(
    'ANTARES_OBJECT_URL_TEMPLATE',
    'https://antares.noirlab.edu/loci?search={diaObjectId}',
)

######################################################################
# Database
#
# Persist DB connections across work units and validate a reused connection
# before handing it out. The broker consumers recycle connections explicitly via
# close_old_connections() (see brokers.ingest_alert); with health checks on, a
# reused connection that Postgres has severed is transparently reopened instead
# of raising "the connection is closed", and CONN_MAX_AGE avoids reconnecting on
# every alert. Set CONN_MAX_AGE=0 to restore Django's close-after-each-use.
DATABASES = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE', 'django.db.backends.postgresql'),
        'NAME': os.getenv('DATABASE_DB', 'scimma_crossmatch_service'),
        'USER': os.getenv('DATABASE_USER', 'crossmatch_service_admin'),
        'PASSWORD': os.getenv('DATABASE_PASSWORD', 'password'),
        'HOST': os.getenv('DATABASE_HOST', '127.0.0.1'),
        'PORT': os.getenv('DATABASE_PORT', '5432'),
        'CONN_MAX_AGE': int(os.getenv('CONN_MAX_AGE', '60')),
        'CONN_HEALTH_CHECKS': True,
        # libpq connect_timeout, web tier only: entrypoints/run_web.sh exports
        # DATABASE_CONNECT_TIMEOUT so an unreachable database fails fast into the
        # API's structured 503 (KTD12) instead of outliving the request. Unset in
        # every other process (Celery, beat, ingest consumers), which keep
        # libpq's default wait-for-TCP behavior.
        'OPTIONS': (
            {'connect_timeout': int(os.environ['DATABASE_CONNECT_TIMEOUT'])}
            if os.environ.get('DATABASE_CONNECT_TIMEOUT')
            else {}
        ),
    },
    'sqlite': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(APP_ROOT_DIR, 'db.sqlite3'),
    },
}
# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

######################################################################
# Webserver config
#
# HTTP entry point (read-model API + admin). Only the web workload
# (entrypoints/run_web.sh -> gunicorn project.wsgi:application) uses these; the
# Celery workers and ingest consumers never serve HTTP.
ROOT_URLCONF = 'project.urls'
WSGI_APPLICATION = 'project.wsgi.application'
# DJANGO_ALLOWED_HOSTS is a comma-separated list, supplied to the web pod by the
# gitops web.env helper (from .Values.ingress.host). The default includes the
# DEV host so a bare local/dev boot serves without a DisallowedHost 400.
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get(
        'DJANGO_ALLOWED_HOSTS', 'crossmatch-dev.scimma.org,localhost,127.0.0.1'
    ).split(',')
    if h.strip()
]

# Recent-crossmatch API server-side ceilings. The endpoint is unauthenticated on
# DEV, so these bound the work any single request can trigger. Results are keyset
# (cursor) paged: MAX_PAGE_SIZE caps the objects one request/page may return (a
# caller page_size only narrows below it, and an over-max page_size clamps down
# rather than being rejected), DEFAULT_PAGE_SIZE is the page size used when the
# caller omits one, and MAX_WINDOW_HOURS rejects a window span larger than this.
# There is intentionally no total cap on how many objects a window can be paged
# through -- per-page work is bounded, total iteration is not (rate limiting is
# deferred with the accepted public-on-DEV posture).
RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE = int(
    os.environ.get('RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE', '1000')
)
RECENT_CROSSMATCH_MAX_PAGE_SIZE = int(
    os.environ.get('RECENT_CROSSMATCH_MAX_PAGE_SIZE', '10000')
)
RECENT_CROSSMATCH_MAX_WINDOW_HOURS = int(
    os.environ.get('RECENT_CROSSMATCH_MAX_WINDOW_HOURS', '168')
)

# Object and position API per-request cost bound (KTD12, R32). The edge rate
# limit caps only request rate, so each request's work is bounded here:
#   - API_REQUEST_BUDGET_SECONDS is the wall-clock budget of one guarded API
#     request (api/guard.py): SQL runs in a read-only transaction whose
#     statement_timeout is reset to the time remaining before each SQL phase,
#     and an overrun returns 400 query_too_expensive (not retryable).
#   - The maximums below are set together so the largest allowed request fits
#     the budget: 1000 IDs, 200 positions, a 60 arcsec cone radius, 100 objects
#     per batched position, and 5000 objects per request across all inputs.
# All are starting values: the pre-release benchmark on DEV (plan Verification
# Contract) confirms or lowers each so its p95 is at most half the budget. They
# are read at call time, so an env override needs only a pod restart.
# Worker model (entrypoints/run_web.sh): gunicorn gthread workers,
# WEB_WORKERS=2 x WEB_THREADS=4 = 8 concurrent requests per web pod. Each thread
# holds at most one Postgres connection (CONN_MAX_AGE persists it), so one web
# replica uses at most 8 connections -- well inside Postgres's default
# max_connections=100 alongside Celery and the ingest consumers. With a 5 s
# budget, a pod saturated by worst-case API requests still frees a thread every
# ~5/8 s, so /healthz and the HTML pages are not queued behind them; gunicorn's
# --timeout (WEB_TIMEOUT, 30 s) is only the worker-heartbeat backstop, well above
# the budget, and the web-only DATABASE_CONNECT_TIMEOUT (3 s) keeps an
# unreachable database inside it.
API_REQUEST_BUDGET_SECONDS = float(os.environ.get('API_REQUEST_BUDGET_SECONDS', '5'))
API_MAX_IDS = int(os.environ.get('API_MAX_IDS', '1000'))
API_MAX_POSITIONS = int(os.environ.get('API_MAX_POSITIONS', '200'))
API_MAX_CONE_RADIUS_ARCSEC = float(os.environ.get('API_MAX_CONE_RADIUS_ARCSEC', '60'))
API_MAX_OBJECTS_PER_POSITION = int(
    os.environ.get('API_MAX_OBJECTS_PER_POSITION', '100')
)
API_MAX_OBJECTS_PER_REQUEST = int(
    os.environ.get('API_MAX_OBJECTS_PER_REQUEST', '5000')
)

# MCP chat connector caps (KTD9), read at call time like the API maximums.
#   - MCP_MAX_IDENTIFIERS: identifiers one lookup call accepts; above the chat
#     maximum, so a longer list is truncated with an API recipe, not refused.
#   - MCP_MAX_OBJECTS: object summaries per answer (R1's chat maximum); one TNS
#     name can resolve to several objects, and each counts.
#   - MCP_MATCHES_PER_CATALOG: nearest coincident sources shown per catalog,
#     plus a count of the rest.
#   - MCP_MAX_RESULT_CHARS: size of the serialized answer; whole objects are
#     dropped from the end to meet it, and any drop counts as truncation. 20
#     typical summaries (one Gaia and one DES source each, key values, links)
#     measure about 23,000 characters, so the default leaves room for TNS
#     associations and extra brokers without cutting a typical answer.
#   - MCP_API_BASE_URL: scheme and host of the public API, used in the
#     ready-to-run request a truncated answer carries (request-independent).
MCP_MAX_IDENTIFIERS = int(os.environ.get('MCP_MAX_IDENTIFIERS', '100'))
MCP_MAX_OBJECTS = int(os.environ.get('MCP_MAX_OBJECTS', '20'))
MCP_MATCHES_PER_CATALOG = int(os.environ.get('MCP_MATCHES_PER_CATALOG', '3'))
MCP_MAX_RESULT_CHARS = int(os.environ.get('MCP_MAX_RESULT_CHARS', '30000'))
MCP_API_BASE_URL = os.environ.get('MCP_API_BASE_URL', 'https://crossmatch.scimma.org')
# MCP endpoint transport (KTD3, KTD13).
#   - MCP_ALLOWED_ORIGINS: comma-separated browser origins allowed to call
#     /mcp. A request carrying any other Origin header gets 403 (the transport
#     spec's DNS-rebinding guard); requests with no Origin, which is how the
#     chat providers' back ends call, always pass. Empty by default.
#   - MCP_SESSION_MAX_AGE_SECONDS: how long a signed Mcp-Session-Id stays
#     valid. An expired or invalid ID is still served, only without a
#     per-conversation key (the rate-limit fallback bucket).
MCP_ALLOWED_ORIGINS = tuple(
    origin.strip()
    for origin in os.environ.get('MCP_ALLOWED_ORIGINS', '').split(',')
    if origin.strip()
)
MCP_SESSION_MAX_AGE_SECONDS = int(os.environ.get('MCP_SESSION_MAX_AGE_SECONDS', '86400'))
# MCP rate limits and concurrency cap (KTD4), read at call time; only
# tools/call is limited, on the shared cache (Valkey). A limited call is an
# isError tool result with a retry-after, never an HTTP 429.
#   - MCP_CLIENT_IP_HEADER: request header carrying the client IP (Traefik
#     sets X-Real-Ip); REMOTE_ADDR when it is absent.
#   - MCP_PROVIDER_CIDRS: comma-separated chat-provider egress ranges.
#     Callers inside them are limited per session ID; with no valid session ID
#     they share one bucket per range. Default: Anthropic's 160.79.104.0/21.
#   - Rates are "<requests per second>/<burst>":
#     MCP_SESSION_RATE per provider session, MCP_IP_RATE per other client IP
#     (both the API query routes' 2/s burst 10), and MCP_PROVIDER_RATE for a
#     provider range's session-less calls: 10/s burst 50, five conversations'
#     worth, because every user of a client that does not echo the session ID
#     (or of the session-less 2026-07-28 protocol) lands in it.
#   - MCP_MAX_CONCURRENT: tool calls in flight cluster-wide (6 of the 16 PROD
#     web slots); MCP_MAX_CONCURRENT_PER_KEY: in flight per session or client,
#     so one conversation cannot hold every slot.
def _mcp_rate(name, default):
    rate, burst = os.environ.get(name, default).split('/')
    return (float(rate), int(burst))


MCP_CLIENT_IP_HEADER = os.environ.get('MCP_CLIENT_IP_HEADER', 'X-Real-Ip')
MCP_PROVIDER_CIDRS = tuple(
    cidr.strip()
    for cidr in os.environ.get('MCP_PROVIDER_CIDRS', '160.79.104.0/21').split(',')
    if cidr.strip()
)
MCP_SESSION_RATE = _mcp_rate('MCP_SESSION_RATE', '2/10')
MCP_IP_RATE = _mcp_rate('MCP_IP_RATE', '2/10')
MCP_PROVIDER_RATE = _mcp_rate('MCP_PROVIDER_RATE', '10/50')
MCP_MAX_CONCURRENT = int(os.environ.get('MCP_MAX_CONCURRENT', '6'))
MCP_MAX_CONCURRENT_PER_KEY = int(os.environ.get('MCP_MAX_CONCURRENT_PER_KEY', '2'))

# Declared broker-enforced reliability cuts (R20, KTD8). ANTARES and Lasair
# apply their own reliability filter before alerts reach this service, so the
# service cannot observe the value; the maintainer declares it here with the
# date it was confirmed (ISO YYYY-MM-DD). Unset reports ``not_declared``. A value
# and its as-of date are set together or not at all. Pitt-Google's cut is
# MIN_DIASOURCE_RELIABILITY, enforced by this service, and needs no declaration.
# Read only through core/provenance.py.


def _declared_cut(broker):
    """Parse and validate one broker's declared reliability cut from the env.

    Args:
        broker: The env-var prefix, e.g. ``'LASAIR'``.

    Returns:
        ``(value, as_of)``: a float in [0, 1] and an ISO date string, or
        ``(None, None)`` when neither is set.

    Raises:
        ImproperlyConfigured: If only one of the pair is set, the value is not a
            finite float in [0, 1], or the date is not an ISO ``YYYY-MM-DD``.
    """
    value_var = f'{broker}_DECLARED_MIN_RELIABILITY'
    as_of_var = f'{value_var}_AS_OF'
    raw_value = os.environ.get(value_var, '').strip()
    raw_as_of = os.environ.get(as_of_var, '').strip()
    if not raw_value and not raw_as_of:
        return None, None
    if not (raw_value and raw_as_of):
        raise ImproperlyConfigured(
            f'{value_var} and {as_of_var} must be set together (got '
            f'{raw_value!r} / {raw_as_of!r})'
        )
    try:
        value = float(raw_value)
    except ValueError:
        value = float('nan')
    if not (0.0 <= value <= 1.0):
        raise ImproperlyConfigured(
            f'{value_var} must be a finite float in [0.0, 1.0]; got {raw_value!r}'
        )
    try:
        as_of = datetime.date.fromisoformat(raw_as_of)
    except ValueError:
        as_of = None
    if as_of is None or as_of.isoformat() != raw_as_of:
        raise ImproperlyConfigured(
            f'{as_of_var} must be an ISO date (YYYY-MM-DD); got {raw_as_of!r}'
        )
    return value, as_of.isoformat()


ANTARES_DECLARED_MIN_RELIABILITY, ANTARES_DECLARED_MIN_RELIABILITY_AS_OF = (
    _declared_cut('ANTARES')
)
LASAIR_DECLARED_MIN_RELIABILITY, LASAIR_DECLARED_MIN_RELIABILITY_AS_OF = (
    _declared_cut('LASAIR')
)

# Password validation
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(APP_ROOT_DIR, 'static')

# WhiteNoise serves collected static from the gunicorn pod (no nginx). Use the
# compressed (non-manifest) backend so `{% static %}` resolves without a
# staticfiles.json manifest -- the informational site does not need hashed asset
# URLs, and this keeps template rendering working in tests/dev before
# collectstatic has run.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}
# USE_FINDERS lets WhiteNoise serve app static dirs directly, so /static/web/...
# resolves under pytest and the bare dev server, which never run collectstatic.
# It defaults on for that convenience; deployments that DO run collectstatic
# (entrypoints/run_web.sh) set WHITENOISE_USE_FINDERS=false so WhiteNoise serves
# the collected, precompressed STATIC_ROOT via a prebuilt index instead of
# walking the finder tree on every request.
WHITENOISE_USE_FINDERS = (
    os.environ.get('WHITENOISE_USE_FINDERS', 'true').lower() == 'true'
)

# Footer "Contact us" mailto target (U2 support_email tag; footer content, not a
# live-config seam field). Overridable per deployment.
SUPPORT_EMAIL = os.environ.get(
    'SUPPORT_EMAIL', 'scimma-crossmatch@lists.scimma.org'
)

######################################################################
# Logging config
#
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        }
    },
    'loggers': {
        # '': {
        #     'handlers': ['console'],
        #     'level': 'INFO'
        # },
        'mozilla_django_oidc': {
            'handlers': ['console'],
            'level': 'DEBUG'
        },
    }
}
logging.config.dictConfig(LOGGING)
