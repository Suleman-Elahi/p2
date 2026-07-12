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
from datetime import timedelta

from p2 import __version__
from p2.lib.config import CONFIG
from django.core.exceptions import ImproperlyConfigured

# Compat shim removed since we are no longer using django.contrib.postgres.

# ---------------------------------------------------------------------------
# Base paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

SECRET_KEY = CONFIG.y('secret_key', '')
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY is not set. Add SECRET_KEY=<random-string> to your .env file. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )

# Fernet key for reversible encryption of API key secrets (used in AWS v4 HMAC auth).
# Must be a URL-safe base64-encoded 32-byte key. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY = CONFIG.y('fernet_key', '')

# ── Ninja JWT Configuration ────────────────────────────────────────────────
# ninja-jwt is used for the REST API (/api/v1/*) authentication.
# Without this, JWTAuth() cannot validate tokens and returns 401.
NINJA_JWT = {
    'SIGNING_KEY': SECRET_KEY,
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

DEBUG = CONFIG.y_bool('debug')
TEST = any('test' in arg for arg in sys.argv)

# Validate FERNET_KEY early (before DEBUG/TEST are used elsewhere).
# Skip validation during tests since fixtures may not set this.
if (not TEST) and (not FERNET_KEY or str(FERNET_KEY).lower().startswith('change-me')):
    raise ImproperlyConfigured(
        "FERNET_KEY is not set. Add P2_FERNET_KEY=<fernet-key> to your .env file. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
    )

CORS_ORIGIN_ALLOW_ALL = DEBUG
SECURE_SSL_REDIRECT = CONFIG.y_bool('security.ssl_redirect', default=not DEBUG and not TEST)
X_FRAME_OPTIONS = "SAMEORIGIN"

# Set True in production when Nginx handles X-Accel-Redirect (zero-copy reads)
USE_X_ACCEL_REDIRECT = CONFIG.y_bool("storage.use_x_accel_redirect", default=False)
# Publish S3 post-save events without blocking PUT response.
S3_ASYNC_EVENT_PUBLISH = CONFIG.y_bool("s3.async_event_publish", default=False)
# Optional bounded queue for Redis Stream publishing (experimental, off by default).
S3_EVENT_QUEUE_ENABLED = CONFIG.y_bool("s3.event_queue.enabled", default=False)
S3_EVENT_QUEUE_MAX_SIZE = int(CONFIG.y("s3.event_queue.max_size", default=8192))
S3_EVENT_QUEUE_BATCH_SIZE = int(CONFIG.y("s3.event_queue.batch_size", default=64))
S3_EVENT_QUEUE_FLUSH_MS = int(CONFIG.y("s3.event_queue.flush_ms", default=5))
S3_EVENT_QUEUE_WAIT_FOR_ACK = CONFIG.y_bool("s3.event_queue.wait_for_ack", default=False)
S3_BLOB_SHARD_DEPTH = max(1, min(2, int(CONFIG.y("s3.blob.shard_depth", default=2))))
# LMDB metadata durability knobs.
# Default is NON-fsyncing (sync/metasync False): every PUT already issues one
# fdatasync on the volume .bin file (the actual object bytes). Making LMDB also
# fsync on each commit doubles the fsync count on the PUT hot path for no real
# safety gain — the metadata index is fully rebuildable by scanning the volume
# files after a crash (see p2.core.volume_stats.scan_volume_stats). Set these
# True only if you need the LMDB index itself to survive an OS-level crash
# without a rebuild pass.
S3_METADATA_LMDB_SYNC = CONFIG.y_bool("s3.metadata.lmdb.sync", default=False)
S3_METADATA_LMDB_METASYNC = CONFIG.y_bool("s3.metadata.lmdb.metasync", default=False)
# Whether the group committer issues fdatasync on the volume file after each
# batch. True = data is durable on HTTP 200 (S3 semantics). Set False only for
# throughput benchmarks where durability is not required.
S3_VOLUME_FDATASYNC = CONFIG.y_bool("s3.volume.fdatasync", default=True)
# Optional bounded queue for async metadata writes (reduces PUT tail latency under concurrency).
S3_METADATA_WRITE_QUEUE_ENABLED = CONFIG.y_bool("s3.metadata.write_queue.enabled", default=True)
S3_METADATA_WRITE_QUEUE_MAX_SIZE = int(CONFIG.y("s3.metadata.write_queue.max_size", default=8192))
S3_METADATA_WRITE_BATCH_SIZE = int(CONFIG.y("s3.metadata.write_queue.batch_size", default=128))
# Straggler wait before flushing a batch of 1. Default 0: the drain loop already
# coalesces all queued requests; a non-zero wait only adds latency at the low
# per-worker concurrency real deployments see (measured 7.3ms vs 1.3ms/op).
S3_METADATA_WRITE_BATCH_WINDOW_MS = float(CONFIG.y("s3.metadata.write_queue.batch_window_ms", default=0.0))
# In-process hot-path cache TTLs (seconds) for S3 auth/ACL checks.
S3_CACHE_APIKEY_TTL_SECONDS = float(CONFIG.y("s3.cache.apikey_ttl_seconds", default=600))
S3_CACHE_VOLUME_TTL_SECONDS = float(CONFIG.y("s3.cache.volume_ttl_seconds", default=600))
S3_CACHE_ACL_TTL_SECONDS = float(CONFIG.y("s3.cache.acl_ttl_seconds", default=600))
S3_CACHE_VOLUME_PERMISSION_TTL_SECONDS = float(CONFIG.y("s3.cache.volume_permission_ttl_seconds", default=600))
S3_CACHE_METADATA_TTL_SECONDS = float(CONFIG.y("s3.cache.metadata_ttl_seconds", default=60))
S3_CACHE_PREFIX_TTL_SECONDS = float(CONFIG.y("s3.cache.prefix_ttl_seconds", default=30))
# Compression settings for large objects (disabled by default, enable for storage savings).
S3_COMPRESSION_ENABLED = CONFIG.y_bool("s3.compression.enabled", default=False)
S3_COMPRESSION_MIN_SIZE = int(CONFIG.y("s3.compression.min_size", default=1024 * 1024))  # 1MB
S3_COMPRESSION_LEVEL = int(CONFIG.y("s3.compression.level", default=6))  # zlib level 1-9
# Base directory for LMDB volume data. Override with P2_STORAGE__ROOT for local dev.
STORAGE_ROOT = CONFIG.y("storage.root", default="/storage")

# ---------------------------------------------------------------------------
# Volume Pool (Fixed-Size Preallocated .bin files)
# ---------------------------------------------------------------------------
# Max size of a single volume file before it is sealed (default 10 GiB).
VOLUME_SIZE_BYTES = int(CONFIG.y("storage.volume_size_bytes", default=10 * 1024 * 1024 * 1024))
# Number of concurrently active write volumes per process.
VOLUME_ACTIVE_POOL_SIZE = max(1, int(CONFIG.y("storage.volume_active_pool_size", default=4)))
# Sealed volume live-byte ratio below which compaction triggers (0.0–1.0).
VOLUME_COMPACT_THRESHOLD = float(CONFIG.y("storage.volume_compact_threshold", default=0.30))
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

_allowed_hosts = CONFIG.y('allowed_hosts', 'localhost,127.0.0.1,[::1]')
ALLOWED_HOSTS = ['*'] if DEBUG else [host.strip() for host in str(_allowed_hosts).split(',') if host.strip()]
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
    'ninja',
    'ninja_jwt',
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
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.http.ConditionalGetMiddleware',
    'p2.api.middleware.ApiCSRFExemptMiddleware',  # skip CSRF for JWT-based API paths
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'p2.root.urls'
APPEND_SLASH = False

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
SESSION_COOKIE_SECURE = CONFIG.y_bool('security.session_cookie_secure', default=not DEBUG and not TEST)
CSRF_COOKIE_SECURE = CONFIG.y_bool('security.csrf_cookie_secure', default=not DEBUG and not TEST)

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
# REST Framework (Removed - Using Django Ninja)
# ---------------------------------------------------------------------------

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
        'p2.s3.middleware': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'p2.s3.views.objects': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'p2.s3.engine': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'p2': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.contrib.sessions': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'arq': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'grpc': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}

# ---------------------------------------------------------------------------
# Test overrides
# ---------------------------------------------------------------------------

if TEST:
    VOLUME_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB for tests (prevents huge allocations via posix_fallocate)
    VOLUME_ACTIVE_POOL_SIZE = 2           # 2 active volumes for tests

# ---------------------------------------------------------------------------
# Debug toolbar (dev only)
# ---------------------------------------------------------------------------

if DEBUG:
    INTERNAL_IPS = ['127.0.0.1', '::1']
    try:
        import debug_toolbar  # noqa: F401
        import django_extensions  # noqa: F401
        INSTALLED_APPS += ['debug_toolbar', 'django_extensions']
        MIDDLEWARE.append('debug_toolbar.middleware.DebugToolbarMiddleware')
    except ImportError:
        pass
