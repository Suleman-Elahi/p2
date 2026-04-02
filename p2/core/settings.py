"""
Django settings for p2 project (Django 5.x / async-first).

Replaces p2/root/settings.py with modernized configuration:
- Django 5.x native JSONField (no postgres-specific import)
- ASGI / uvicorn entrypoint
- psycopg 3.x async-capable database engine
- Django built-in Redis cache backend (replaces django-redis)
- ARQ task queue settings (replaces Celery)
- OpenTelemetry configuration
- djangorestframework-simplejwt (replaces djangorestframework-jwt)
- drf-spectacular (replaces drf-yasg)
- authlib OIDC (replaces mozilla-django-oidc)
- VolumeACL permission model (replaces django-guardian)
"""

import os
import sys

from p2 import __version__
from p2.lib.config import CONFIG

# Compat shim removed since we are no longer using django.contrib.postgres.

# ---------------------------------------------------------------------------
# Base paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

SECRET_KEY = CONFIG.y('secret_key', '48e9z8tw=_z0e#m*x70&)u%cgo8#=16uzdze&i8q=*#**)@cp&')  # noqa

# Fernet key for reversible encryption of API key secrets (used in AWS v4 HMAC auth).
# Must be a URL-safe base64-encoded 32-byte key. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY = CONFIG.y('fernet_key', '')

DEBUG = CONFIG.y_bool('debug')
TEST = any('test' in arg for arg in sys.argv)

CORS_ORIGIN_ALLOW_ALL = DEBUG
SECURE_SSL_REDIRECT = False
X_FRAME_OPTIONS = "SAMEORIGIN"

# Set True in production when Nginx handles X-Accel-Redirect (zero-copy reads)
USE_X_ACCEL_REDIRECT = CONFIG.y_bool("storage.use_x_accel_redirect", default=False)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

ALLOWED_HOSTS = ['*']
INTERNAL_IPS = ['127.0.0.1']

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'drf_spectacular',
    'django_filters',
    'crispy_forms',
    'crispy_bootstrap4',
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

MIDDLEWARE = [
    'p2.s3.middleware.S3RoutingMiddleware',  # MUST be first - handles S3 auth + routing
    'p2.core.middleware.HealthCheckMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'p2.core.middleware.S3AuthPreserveMiddleware',
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

ASGI_APPLICATION = 'p2.core.asgi.application'

DATA_UPLOAD_MAX_MEMORY_SIZE = 536870912

# ---------------------------------------------------------------------------
# Database — Turso (libSQL) async-capable engine
# ---------------------------------------------------------------------------

_libsql_sync_url = CONFIG.y('libsql.sync_url', '')
_db_path = CONFIG.y('libsql.file', os.path.join(BASE_DIR, 'p2-control.db'))

if _libsql_sync_url:
    # Turso / embedded replica mode — needs file:// URI
    DATABASES = {
        'default': {
            'ENGINE': 'libsql.db.backends.sqlite3',
            'NAME': f'file:{_db_path}',
            'OPTIONS': {
                'sync_url': _libsql_sync_url,
                'auth_token': CONFIG.y('libsql.auth_token', ''),
            },
        }
    }
else:
    # Local dev — plain SQLite (no Turso connection needed)
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': _db_path,
        }
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Cache — Django 5.x built-in Redis backend (replaces django-redis)
# ---------------------------------------------------------------------------

_redis_password = CONFIG.y('redis.password', '')
_redis_auth = f':{_redis_password}@' if _redis_password else ''
REDIS_URL = CONFIG.y(
    'redis.url',
    f"redis://{_redis_auth}{CONFIG.y('redis.host', 'localhost')}:6379"
    f"/{CONFIG.y('redis.cache_db', '0')}"
)

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
    }
}

SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_SAVE_EVERY_REQUEST = False  # Only save when session data actually changes
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = False

CSRF_TRUSTED_ORIGINS = CONFIG.y('csrf_trusted_origins', 'http://localhost,http://127.0.0.1').split(',')

# ---------------------------------------------------------------------------
# ARQ task queue (replaces Celery)
# ---------------------------------------------------------------------------

ARQ_REDIS_URL = CONFIG.y(
    'redis.arq_url',
    f"redis://{_redis_auth}{CONFIG.y('redis.host', 'localhost')}:6379"
    f"/{CONFIG.y('redis.message_queue_db', '1')}"
)

ARQ_WORKER_SETTINGS = {
    'max_jobs': 50,
    'job_timeout': 300,
    'max_tries': 5,
}

# ---------------------------------------------------------------------------
# OpenTelemetry
# ---------------------------------------------------------------------------

OTEL_ENDPOINT = CONFIG.y('otel.endpoint', os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://localhost:4317'))
OTEL_SERVICE_NAME = CONFIG.y('otel.service_name', os.getenv('OTEL_SERVICE_NAME', 'p2'))

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]

LOGIN_URL = 'auth_login'
LOGIN_REDIRECT_URL = '/'

# authlib OIDC configuration (replaces mozilla-django-oidc)
OIDC_ENABLED = CONFIG.y_bool('oidc.enabled')
AUTHLIB_OAUTH_CLIENTS = {
    'oidc': {
        'client_id': CONFIG.y('oidc.client_id', ''),
        'client_secret': CONFIG.y('oidc.client_secret', ''),
        # OIDC Discovery endpoint — authlib resolves all endpoints automatically
        'server_metadata_url': CONFIG.y('oidc.discovery_url', ''),
        'client_kwargs': {
            'scope': 'openid email profile',
            'code_challenge_method': 'S256',  # PKCE
        },
    }
}

# ---------------------------------------------------------------------------
# REST Framework — simplejwt + spectacular
# ---------------------------------------------------------------------------

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
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'p2 API',
    'DESCRIPTION': 'p2 S3-compatible object storage API',
    'VERSION': __version__,
    'SERVE_INCLUDE_SCHEMA': False,
    'SECURITY': [{'jwtAuth': []}],
    'COMPONENTS': {
        'securitySchemes': {
            'jwtAuth': {
                'type': 'http',
                'scheme': 'bearer',
                'bearerFormat': 'JWT',
            }
        }
    },
}

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
SITE_ID = 1

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = '/_/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static/')

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

CRISPY_TEMPLATE_PACK = 'bootstrap4'
CRISPY_ALLOWED_TEMPLATE_PACKS = 'bootstrap4'

VERSION = __version__

# ---------------------------------------------------------------------------
# Logging (stdlib — OTel LoggingInstrumentor will correlate with traces)
# ---------------------------------------------------------------------------

import warnings
warnings.filterwarnings("ignore", message="StreamingHttpResponse must consume synchronous iterators")

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
        'p2.s3.middleware': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
        'p2': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.contrib.sessions': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
        'arq': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'grpc': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}

# ---------------------------------------------------------------------------
# Test overrides
# ---------------------------------------------------------------------------

if TEST:
    LOGGING = {'version': 1, 'disable_existing_loggers': True}

# ---------------------------------------------------------------------------
# Debug toolbar (dev only)
# ---------------------------------------------------------------------------

if DEBUG:
    try:
        import debug_toolbar  # noqa: F401
        import django_extensions  # noqa: F401
        INSTALLED_APPS += ['debug_toolbar', 'django_extensions']
        MIDDLEWARE.append('debug_toolbar.middleware.DebugToolbarMiddleware')
    except ImportError:
        pass
