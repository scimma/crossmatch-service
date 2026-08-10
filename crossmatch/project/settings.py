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
CROSSMATCH_RADIUS_ARCSEC = float(os.getenv('CROSSMATCH_RADIUS_ARCSEC', '1.0'))

CROSSMATCH_CATALOGS = [
    {
        'name': 'gaia_dr3',
        'hats_url': GAIA_HATS_URL,
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
    },
    {
        'name': 'des_y6_gold',
        'hats_url': DES_HATS_URL,
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
    },
    {
        'name': 'delve_dr3_gold',
        'hats_url': DELVE_HATS_URL,
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
    },
    {
        'name': 'skymapper_dr4',
        'hats_url': SKYMAPPER_HATS_URL,
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
    },
]

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
