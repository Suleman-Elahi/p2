"""
Django settings for p2 project — LEGACY FILE.

This file is superseded by p2/core/settings.py (Django 5.x / async-first).
It is retained only for reference. The active settings module is p2.core.settings.

All deprecated dependencies have been removed:
- guardian → VolumeACL (p2.core.acl)
- django_prometheus → OpenTelemetry (p2.core.telemetry)
- mozilla_django_oidc → authlib
- drf_yasg → drf-spectacular
- rest_framework_jwt → rest_framework_simplejwt
- Celery → arq (p2.core.worker)
- structlog → stdlib logging + OpenTelemetry
"""

import os
import sys

from p2 import __version__
from p2.lib.config import CONFIG

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECRET_KEY = CONFIG.y('secret_key', '')  # Must be set in .env

DEBUG = CONFIG.y_bool('debug')
TEST = any('test' in arg for arg in sys.argv)
CORS_ORIGIN_ALLOW_ALL = DEBUG

SECURE_SSL_REDIRECT = not DEBUG and not TEST
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

ALLOWED_HOSTS = ['*']
INTERNAL_IPS = ['127.0.0.1']

REST_FRAMEWORK = {
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.LimitOffsetPagination',
    'PAGE_SIZE': 100,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.OrderingFilter',
        'rest_framework.filters.SearchFilter',
    ],
    'DEFAULT_PERMISSION_CLASSES': (
        'p2.api.permissions.CustomObjectPermissions',
    ),
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
}

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': (
            f"redis://:{CONFIG.y('redis.password', '')}@{CONFIG.y('redis.host', 'localhost')}"
            f":6379/{CONFIG.y('redis.cache_db', '0')}"
        ),
    }
}

SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'django.contrib.postgres',
    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'drf_spectacular',
    'django_filters',
    'crispy_forms',
    # p2 - Core Components
    'p2.core.apps.P2CoreConfig',
    'p2.api.apps.P2APIConfig',
    'p2.s3.apps.P2S3Config',
    'p2.serve.apps.P2ServeConfig',
    'p2.log.apps.P2LogConfig',
    'p2.ui.apps.P2UIConfig',
    # p2 - Components
    'p2.components.quota.apps.P2QuotaComponentConfig',
    'p2.components.image.apps.P2ImageComponentConfig',
    'p2.components.public_access.apps.P2PublicAccessComponentConfig',
    'p2.components.replication.apps.P2ReplicationComponentConfig',
    'p2.components.expire.apps.P2ExpireComponentConfig',
    # p2 - Storage
    'p2.storage.local.apps.P2LocalStorageConfig',
    'p2.storage.s3.apps.P2S3StorageConfig',
]

LOGIN_URL = 'auth_login'
LOGIN_REDIRECT_URL = '/'

CRISPY_TEMPLATE_PACK = 'bootstrap4'

OIDC_ENABLED = CONFIG.y_bool('oidc.enabled')
AUTHLIB_OAUTH_CLIENTS = {
    'oidc': {
        'client_id': CONFIG.y('oidc.client_id', ''),
        'client_secret': CONFIG.y('oidc.client_secret', ''),
        'server_metadata_url': CONFIG.y('oidc.discovery_url', ''),
        'client_kwargs': {
            'scope': 'openid email profile',
            'code_challenge_method': 'S256',
        },
    }
}

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'p2.s3.middleware.S3RoutingMiddleware',
    'p2.core.middleware.HealthCheckMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.http.ConditionalGetMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'p2.root.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            os.path.join(BASE_DIR, 'p2/ui/templates/'),
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'p2.ui.context_processors.version',
            ],
        },
    },
]

VERSION = __version__

WSGI_APPLICATION = 'p2.root.wsgi.application'

DATA_UPLOAD_MAX_MEMORY_SIZE = 536870912

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': CONFIG.y('postgresql.host', 'localhost'),
        'NAME': CONFIG.y('postgresql.name', 'p2'),
        'USER': CONFIG.y('postgresql.user', 'p2'),
        'PASSWORD': CONFIG.y('postgresql.password', ''),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
SITE_ID = 1

STATIC_URL = '/_/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static/')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            'format': '%(asctime)s %(name)s %(levelname)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'p2': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'arq': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'grpc': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
    },
}

if TEST:
    LOGGING = {'version': 1, 'disable_existing_loggers': True}

if DEBUG:
    try:
        import debug_toolbar  # noqa: F401
        import django_extensions  # noqa: F401
        INSTALLED_APPS += ['debug_toolbar', 'django_extensions']
        MIDDLEWARE.append('debug_toolbar.middleware.DebugToolbarMiddleware')
    except ImportError:
        pass
