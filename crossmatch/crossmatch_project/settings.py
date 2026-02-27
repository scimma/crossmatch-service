from pathlib import Path
import logging.config
import os
import json
from corsheaders.defaults import default_headers

django_base_dir = Path(__file__).resolve().parent.parent

APP_VERSION = '0.0.0'

# Django base settings
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-dummy-secret')
DJANGO_SUPERUSER_USERNAME = os.getenv('DJANGO_SUPERUSER_USERNAME', 'admin')
APP_ROOT_DIR = os.environ.get('APP_ROOT_DIR', '/opt')
assert os.path.isabs(APP_ROOT_DIR)
S3_BASE_DIR = os.environ.get('S3_BASE_DIR', '').strip('/')

# Celery settings are loaded from CELERY_ prefix variabled in "celery.py"
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv('CELERY_TASK_SOFT_TIME_LIMIT', "3600"))
CELERY_TASK_TIME_LIMIT = int(os.getenv('CELERY_TASK_TIME_LIMIT', "3800"))

# Application definition

SITE_ID = 1

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_filters',
    'django_bootstrap5',
    'django_celery_results',
    'django_celery_beat',
    'storages',
    'alert_consumer',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.common.CommonMiddleware',
]

# Database
# https://docs.djangoproject.com/en/dev/ref/settings/#databases

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
# https://docs.djangoproject.com/en/dev/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

# Password validation
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# SESSION_ENGINE
# ref: https://docs.djangoproject.com/en/dev/topics/http/sessions/
SESSION_ENGINE = "django.contrib.sessions.backends.cache"

# Internationalization
# https://docs.djangoproject.com/en/2.1/topics/i18n/

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_L10N = False
USE_TZ = True
DATETIME_FORMAT = 'Y-m-d H:m:s'
DATE_FORMAT = 'Y-m-d'


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/dev/howto/static-files/

STATIC_URL = '/static/'
# STATIC_ROOT tells collectstatic where to copy all the static files that it collects.
STATIC_ROOT = os.path.join(APP_ROOT_DIR, 'static')

MEDIA_URL = '/uploads/'
MEDIA_ROOT = os.path.join(APP_ROOT_DIR, 'uploads')

# Caching
# https://docs.djangoproject.com/en/dev/topics/cache/#filesystem-caching

REDIS_SERVICE = os.environ.get('REDIS_SERVICE', 'redis')
CACHES = {
    'default': {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"redis://{REDIS_SERVICE}:6379",
    }
}

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
