import logging.config
import os


######################################################################
# Application config
#
APP_VERSION = '0.0.0'
QUERY_HEROIC_INTERVAL = int(os.getenv('QUERY_HEROIC_INTERVAL', 3600))

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
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
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
        'DIRS': [
            os.path.join(APP_ROOT_DIR, 'cutout/templates'),
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                "cutout.context_processors.user_profile",
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
    "tasks.tasks",
]
CELERY_TASK_ROUTES = {}
CELERY_TASK_DEFAULT_QUEUE = 'alerts'
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

######################################################################
# Database
#
DATABASES = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE', 'django.db.backends.postgresql'),
        'NAME': os.getenv('DATABASE_DB', 'postgres'),
        'USER': os.getenv('DATABASE_USER', 'postgres'),
        'PASSWORD': os.getenv('DATABASE_PASSWORD', 'postgres'),
        'HOST': os.getenv('DATABASE_HOST', '127.0.0.1'),
        'PORT': os.getenv('DATABASE_PORT', '5432'),
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
