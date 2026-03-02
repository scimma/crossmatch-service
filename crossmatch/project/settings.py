import logging.config
import os
import structlog


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
REDIS_SERVICE = os.environ.get('REDIS_SERVICE', 'redis')
REDIS_PORT = int(os.environ.get('REDIS_PORT', '6379'))
# If running Redis in high-availability mode using Sentinel, there must be a master group name set
REDIS_MASTER_GROUP_NAME = os.environ.get('REDIS_MASTER_GROUP_NAME', '')
REDIS_OR_SENTINEL = 'sentinel' if REDIS_MASTER_GROUP_NAME else 'redis'
# Caching config
CACHES = {
    'default': {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"{REDIS_OR_SENTINEL}://{REDIS_SERVICE}:{REDIS_PORT}",
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
# Celery settings are loaded from CELERY_ prefix variabled in "celery.py"
# Results backend
CELERY_RESULT_BACKEND = f"{REDIS_OR_SENTINEL}://{REDIS_SERVICE}:{REDIS_PORT}/{os.getenv('REDIS_RESULT_DB', '1')}"
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {
    'master_name': REDIS_MASTER_GROUP_NAME,
    'retry_policy': {
        'timeout': 5.0
    }
}
CELERYD_REDIRECT_STDOUTS_LEVEL = "INFO"

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
LOG_LEVEL = os.getenv('LOG_LEVEL', 'WARNING')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        }
    },
    'root': {
        'handlers': ['console'],
        'level': LOG_LEVEL,
    },
}
logging.config.dictConfig(LOGGING)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt='iso'),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
