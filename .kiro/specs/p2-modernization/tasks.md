# Implementation Plan: p2 Modernization

## Overview

This plan modernizes p2 from Django 2.2 / sync / Celery to Django 5.x / async-first / arq. Tasks are ordered by dependency: foundational infrastructure first (packaging, Django upgrade, models), then async core (storage backends, event bus, task queue), then the layers that consume them (S3 views, gRPC, components), then cross-cutting concerns (auth, observability), and finally deployment.

## Tasks

- [x] 1. Project scaffolding and dependency modernization
  - [x] 1.1 Replace Pipfile with pyproject.toml using uv
    - Create `pyproject.toml` with all current dependencies updated to target versions
    - Remove `Pipfile` and `Pipfile.lock`
    - Pin Python >= 3.12, Django >= 5.0, DRF >= 3.15
    - Replace `psycopg2` with `psycopg[binary]` 3.x
    - Replace `Pillow==5.2.0` with latest stable Pillow
    - Replace `djangorestframework-jwt` with `djangorestframework-simplejwt`
    - Replace `drf-yasg` with `drf-spectacular`
    - Replace `django-redis` with Django 5.x built-in `django.core.cache.backends.redis.RedisCache`
    - Replace `mozilla-django-oidc` with `authlib`
    - Add `uvicorn[standard]` with `uvloop`
    - Add `arq`, `redis[hiredis]` >= 5.0, `aiofiles`, `aiobotocore`
    - Add `grpcio` >= 1.60, `grpcio-tools`, `protobuf` >= 5.0
    - Add `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-instrumentation-django`
    - Add `hypothesis`, `pytest`, `pytest-asyncio`, `pytest-django`, `fakeredis[aioredis]`, `moto` as dev dependencies
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 10.1, 10.6_

  - [x] 1.2 Update Django settings for 5.x and ASGI
    - Create `p2/core/asgi.py` with uvicorn/uvloop ASGI application entry point
    - Update `p2/core/settings.py`: replace deprecated `JSONField` import, configure async cache backend, update `INSTALLED_APPS` (remove guardian, add simplejwt, spectacular, authlib)
    - Replace `CELERY_*` settings with `ARQ_*` / Redis URL settings
    - Add OpenTelemetry configuration settings (`OTEL_ENDPOINT`, `OTEL_SERVICE_NAME`)
    - Configure `DATABASES` with `psycopg` 3.x engine
    - _Requirements: 1.1, 1.3, 1.9, 10.4, 10.6_


  - [ ]* 1.3 Write unit tests for settings and dependency versions
    - Verify Django >= 5.0, DRF >= 3.15, Python >= 3.12 at import time
    - Verify ASGI application is importable and callable
    - _Requirements: 1.1, 1.2_

- [x] 2. Core model changes and migrations
  - [x] 2.1 Update Volume model with space_used_bytes counter and public_read flag
    - Add `space_used_bytes = models.BigIntegerField(default=0)` to Volume
    - Add `public_read = models.BooleanField(default=False)` to Volume
    - Remove the `cached_property space_used` aggregate query
    - Update `Quota_Controller` to read `volume.space_used_bytes` directly
    - Generate Django migration
    - _Requirements: 5.5, 6.1, 6.4_

  - [x] 2.2 Replace JSONField imports and remove ExportModelOperationsMixin
    - Replace `django.contrib.postgres.fields.JSONField` with `django.db.models.JSONField` in all models (Blob, Volume, Storage, Component)
    - Remove `ExportModelOperationsMixin` from all model classes
    - Generate Django migration for JSONField change
    - _Requirements: 1.3, 9.8_

  - [x] 2.3 Create VolumeACL model
    - Create `p2/core/acl.py` with `VolumeACL` model (volume FK, user FK nullable, group FK nullable, permissions JSONField)
    - Add `unique_together` constraints and database indexes on (volume, user) and (volume, group)
    - Implement `async has_volume_permission(user, volume, permission)` function with public_read check
    - Generate Django migration
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6_

  - [ ]* 2.4 Write property tests for VolumeACL
    - **Property 7: ACL Permission Check with Default-Deny**
    - **Validates: Requirements 5.1, 5.6**
    - **Property 8: Group ACL Inheritance**
    - **Validates: Requirements 5.3**
    - **Property 9: Public-Read Grants Anonymous Read Access**
    - **Validates: Requirements 5.5**

  - [x] 2.5 Implement space_used_bytes atomic updates in Blob save/delete
    - In `Blob.save()`, atomically increment `Volume.space_used_bytes` using `F()` expression when payload is committed
    - In `Blob` pre_delete, atomically decrement `Volume.space_used_bytes` using `F()` expression
    - Handle edge case: decrement should not go below 0
    - _Requirements: 6.2, 6.3_

  - [ ]* 2.6 Write property tests for space_used_bytes invariant
    - **Property 10: Space Used Bytes Invariant**
    - **Validates: Requirements 6.2, 6.3**

  - [x] 2.7 Create recalculate_space_used management command
    - Create `p2/core/management/commands/recalculate_space_used.py`
    - Aggregate actual blob sizes per volume and update `space_used_bytes`
    - _Requirements: 6.5_

  - [ ]* 2.8 Write property test for space recalculation
    - **Property 11: Space Used Recalculation Correctness**
    - **Validates: Requirements 6.5**

  - [x] 2.9 Reduce hash computation to MD5 + SHA256 only
    - Update `blob_payload_hash` handler in `p2/core/signals.py` to compute only MD5 and SHA256
    - Remove `ATTR_BLOB_HASH_SHA1`, `ATTR_BLOB_HASH_SHA384`, `ATTR_BLOB_HASH_SHA512` from `p2/core/constants.py`
    - _Requirements: 7.1, 7.2, 7.3_

  - [ ]* 2.10 Write property test for hash computation
    - **Property 12: Hash Computation Correctness**
    - **Validates: Requirements 7.1, 7.2**

  - [x] 2.11 Update APIKey model for encrypted secret storage
    - Replace `secret_key` CharField with `secret_key_encrypted` CharField
    - Implement `set_secret_key(raw)` using Fernet encryption with server-side key
    - Implement `decrypt_secret_key()` for AWS v4 auth path
    - Generate Django migration
    - _Requirements: 13.6_

  - [ ]* 2.12 Write property test for API key encryption round-trip
    - **Property 22: API Key Secret Encryption Round-Trip**
    - **Validates: Requirements 13.6**

  - [x] 2.13 Create data migrations
    - Migration: backfill `space_used_bytes` from aggregate query for all volumes
    - Migration: set `public_read=True` for volumes with PublicAccessController component enabled
    - Migration: convert django-guardian `UserObjectPermission` entries to `VolumeACL` entries
    - Migration: encrypt existing plaintext `secret_key` values with Fernet, populate `secret_key_encrypted`
    - Migration: remove `blob.p2.io/hash/sha1`, `sha384`, `sha512` from all blob attributes
    - _Requirements: 5.7, 6.1, 7.3, 13.6_

- [x] 3. Checkpoint - Core models
  - Ensure all tests pass, ask the user if questions arise.


- [x] 4. Async storage backends
  - [x] 4.1 Create async storage controller base class
    - Create `AsyncStorageController` in `p2/core/storages/base.py` with async interface: `get_read_stream()`, `commit()`, `delete()`, `collect_attributes()`
    - Define `AsyncIterator[bytes]` as the streaming interface for reads and writes
    - Implement retry decorator with exponential backoff (max 3 attempts) for transient errors
    - _Requirements: 12.1, 12.4, 12.6_

  - [ ]* 4.2 Write property test for storage retry behavior
    - **Property 19: Storage Retry on Transient Errors**
    - **Validates: Requirements 12.6**

  - [x] 4.3 Implement async local storage controller
    - Rewrite `LocalStorageController` using `aiofiles` for all filesystem I/O
    - Replace `python-magic` MIME detection with `mimetypes.guess_type()` from stdlib
    - Implement `get_read_stream()` yielding chunks via `aiofiles`
    - Implement `commit()` writing from async iterator via `aiofiles`
    - _Requirements: 12.2, 12.4, 12.5_

  - [ ]* 4.4 Write property tests for local storage
    - **Property 17: Storage Backend Write/Read Round-Trip** (local)
    - **Validates: Requirements 12.4**
    - **Property 18: MIME Detection Correctness**
    - **Validates: Requirements 12.5**

  - [x] 4.5 Implement async S3 storage controller
    - Rewrite `S3StorageController` using `aiobotocore` session/client
    - Implement `get_read_stream()` using async `get_object()` body iteration
    - Implement `commit()` using async `upload_fileobj()` or multipart upload
    - Implement `delete()` using async `delete_object()`
    - _Requirements: 12.3, 12.4_

  - [ ]* 4.6 Write property tests for S3 storage
    - **Property 17: Storage Backend Write/Read Round-Trip** (S3, using moto)
    - **Validates: Requirements 12.4**

- [x] 5. Event bus (Redis Streams)
  - [x] 5.1 Implement event bus core
    - Create `p2/core/events.py` with `publish_event(stream, event)` and `consume_events(stream, group, consumer, handler)` functions
    - Use `redis.asyncio` client from `redis-py` 5.x
    - Define stream names: `p2:events:blob_post_save`, `p2:events:blob_payload_updated`
    - Define event payload schema: `blob_uuid`, `volume_uuid`, `event_type`, `timestamp`
    - Implement consumer group creation with `XGROUP CREATE` and `mkstream=True`
    - Implement at-least-once delivery via `XREADGROUP` + `XACK`
    - Implement dead-letter stream for poison messages (after 5 failed attempts)
    - _Requirements: 4.1, 4.3, 4.4, 4.5, 4.6, 10.2_

  - [ ]* 5.2 Write property tests for event bus
    - **Property 5: Event Payload Schema Completeness**
    - **Validates: Requirements 4.5**
    - **Property 6: At-Least-Once Event Delivery with Resume**
    - **Validates: Requirements 4.4, 4.6**

  - [x] 5.3 Replace Django signals with event bus publishing
    - Update `Blob.save()` to call `publish_event(STREAM_BLOB_POST_SAVE, ...)` after save
    - Update `Blob.save()` to call `publish_event(STREAM_BLOB_PAYLOAD_UPDATED, ...)` when payload changes
    - Keep `BLOB_PRE_SAVE` as a synchronous in-process call for quota check
    - Remove `BLOB_POST_SAVE` and `BLOB_PAYLOAD_UPDATED` Django signal definitions and receivers
    - Remove `signal_marshall` Celery task
    - _Requirements: 4.1, 4.2, 8.1, 8.2, 8.3_

  - [x] 5.4 Implement event consumers for component handlers
    - Create consumer for hash computation (MD5 + SHA256) on `blob_payload_updated` stream
    - Create consumer for replication metadata on `blob_post_save` stream
    - Create consumer for replication payload on `blob_payload_updated` stream
    - Create consumer for expiry scheduling on `blob_post_save` stream
    - Create consumer for image EXIF extraction on `blob_payload_updated` stream
    - Wire consumers to start as async tasks in the worker process
    - _Requirements: 8.1, 8.2, 8.4_

- [x] 6. Async task queue (arq)
  - [x] 6.1 Replace Celery with arq worker setup
    - Remove `p2/core/celery.py` and all `@CELERY_APP.task` decorators
    - Create `p2/core/worker.py` with arq `WorkerSettings` class
    - Register task functions: `replicate_metadata`, `replicate_payload`, `replicate_delete`, `complete_multipart`, `initial_full_replication`
    - Register cron job: `run_expire` every 60 seconds
    - Configure `redis_settings` from Django settings `REDIS_URL`
    - Configure `max_jobs=50`, `job_timeout=300`, `max_tries=5`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 6.2 Rewrite replication tasks as async arq functions
    - Convert `replicate_metadata_update_task` to async function using async ORM queries
    - Convert `replicate_payload_update_task` to async function using async storage backends
    - Convert `replicate_delete_task` to async function
    - Convert `initial_full_replication` to async function
    - _Requirements: 3.4_

  - [x] 6.3 Rewrite multipart upload completion as async arq task
    - Convert `complete_multipart_upload` from Celery task to async arq function
    - Use async ORM queries and async storage backend for part assembly
    - _Requirements: 3.6_

  - [x] 6.4 Rewrite expiry task as async arq cron function
    - Convert `run_expire` to async function
    - Register as arq cron job (every 60 seconds)
    - Update expiry signal handler to enqueue via arq `pool.enqueue_job()` with `_defer_by` for delayed execution
    - _Requirements: 3.2, 3.3_

  - [ ]* 6.5 Write property test for task retry behavior
    - **Property 3: Task Retry with Exponential Backoff**
    - **Validates: Requirements 3.7, 8.4**

  - [ ]* 6.6 Write property test for delayed task execution
    - **Property 23: Delayed Task Execution Timing**
    - **Validates: Requirements 3.2**

- [x] 7. Checkpoint - Async infrastructure
  - Ensure all tests pass, ask the user if questions arise.


- [x] 8. Async S3 API layer
  - [x] 8.1 Convert S3View base class to async
    - Update `S3View` to use async `dispatch`, async permission checks via `has_volume_permission()`
    - Replace `get_objects_for_user()` guardian calls with `VolumeACL` queries
    - Implement `get_volume()` and `get_blob()` helpers as async methods
    - _Requirements: 2.1, 5.4_

  - [x] 8.2 Convert BucketView to async
    - Rewrite `handler_list`, `put`, `delete` as async methods
    - Use async ORM queries (`afirst()`, `aexists()`, `aiterator()`)
    - Replace `assign_perm` / `get_objects_for_user` with `VolumeACL` operations
    - _Requirements: 2.1, 2.2, 5.1_

  - [x] 8.3 Convert ObjectView to async
    - Rewrite `get`, `put`, `delete`, `head` as async methods
    - Stream response bodies using `StreamingHttpResponse` with async storage `get_read_stream()`
    - Stream request bodies for PUT using async iterator over `request.body` chunks
    - Replace guardian permission checks with `VolumeACL` queries
    - Publish events via event bus after blob save
    - Fire sync pre-save quota check via `sync_to_async` wrapper
    - _Requirements: 2.1, 2.2, 2.3, 2.6, 4.2, 8.3_

  - [x] 8.4 Convert MultipartUploadView to async
    - Rewrite `post_handle_mp_initiate`, `post_handle_mp_complete`, `put` as async methods
    - Replace `complete_multipart_upload.delay()` with arq `pool.enqueue_job()`
    - Replace streaming space-keeping response with async SSE or async streaming response
    - _Requirements: 2.1, 2.4_

  - [x] 8.5 Ensure AWS v4 signature verification is non-blocking
    - Wrap HMAC computation in `sync_to_async` if needed, or verify it's CPU-bound and fast enough to stay sync
    - Ensure `chunked_hasher` for content SHA256 verification works with async request body
    - _Requirements: 2.5_

  - [ ]* 8.6 Write property tests for S3 API
    - **Property 1: S3 Object PUT/GET Round-Trip**
    - **Validates: Requirements 2.3**
    - **Property 2: Multipart Upload Assembly Correctness**
    - **Validates: Requirements 2.4, 3.6**
    - **Property 4: Quota Blocks Over-Threshold Writes**
    - **Validates: Requirements 4.2, 8.3**

- [x] 9. Async gRPC serve layer
  - [x] 9.1 Modernize gRPC server to async
    - Update `p2/serve/grpc.py` to use `grpc.aio.server()` instead of sync `grpc.server()`
    - Rewrite `Serve.RetrieveFile` as async method
    - Use async ORM queries for blob lookups and permission checks via `VolumeACL`
    - Replace `MockRequest` with a proper `RequestContext` dataclass carrying user identity and trace context
    - _Requirements: 11.1, 11.2, 11.5_

  - [x] 9.2 Implement streaming blob data in gRPC responses
    - Stream blob data from async storage backend `get_read_stream()` into gRPC response
    - Avoid reading entire blob into memory
    - _Requirements: 11.3_

  - [x] 9.3 Update protobuf tooling
    - Update proto compilation to use `grpcio-tools` with `protobuf` 5.x
    - Regenerate `serve_pb2.py` and `serve_pb2_grpc.py`
    - _Requirements: 11.4_

  - [ ]* 9.4 Write property test for gRPC serve layer
    - **Property 16: gRPC Serve Returns Correct Blob Data**
    - **Validates: Requirements 11.3**

- [x] 10. Modern authentication
  - [x] 10.1 Integrate authlib for OIDC
    - Replace `mozilla-django-oidc` with `authlib` Django integration
    - Configure OIDC Discovery (`.well-known/openid-configuration`) auto-endpoint resolution
    - Enable PKCE support for authorization code flow
    - Update `p2/root/urls.py` to replace `mozilla_django_oidc.urls` with authlib routes
    - _Requirements: 13.1, 13.2, 13.3_

  - [x] 10.2 Integrate djangorestframework-simplejwt
    - Replace `rest_framework_jwt` views with `simplejwt` `TokenObtainPairView`, `TokenRefreshView`, `TokenVerifyView`
    - Update `p2/api/urls.py` JWT endpoints
    - Replace `drf-yasg` schema view with `drf-spectacular` `SpectacularAPIView`, `SpectacularSwaggerView`, `SpectacularRedocView`
    - _Requirements: 1.6, 1.7, 13.4_

  - [ ]* 10.3 Write property tests for auth
    - **Property 20: JWT Issuance/Validation Round-Trip**
    - **Validates: Requirements 1.6, 13.4**
    - **Property 21: AWS v4 Signature Verification Round-Trip**
    - **Validates: Requirements 13.5**

  - [x] 10.4 Update S3 auth to use encrypted API keys
    - Update `AWSV4Authentication.validate()` to decrypt secret key via Fernet before HMAC computation
    - Update `_lookup_access_key()` to return the decrypted secret
    - _Requirements: 13.5, 13.6_

- [x] 11. Checkpoint - API and auth layers
  - Ensure all tests pass, ask the user if questions arise.


- [x] 12. OpenTelemetry observability
  - [x] 12.1 Set up OpenTelemetry SDK and instrumentation
    - Create `p2/core/telemetry.py` with `setup_telemetry()` function
    - Configure `TracerProvider` with `BatchSpanProcessor` and `OTLPSpanExporter`
    - Configure `MeterProvider` with `OTLPMetricExporter`
    - Instrument Django via `DjangoInstrumentor`
    - Instrument logging via `LoggingInstrumentor` for trace-log correlation
    - Call `setup_telemetry()` from ASGI application startup
    - _Requirements: 9.1, 9.2, 9.6_

  - [x] 12.2 Add custom spans and metrics for S3 API
    - Add trace spans to S3 views with attributes: method, bucket, key, status_code, latency
    - Create `p2.s3.requests` counter and `p2.s3.latency` histogram metrics
    - _Requirements: 9.3, 9.8_

  - [x] 12.3 Add custom spans for task queue and storage operations
    - Wrap arq task execution with OTel spans (task name, duration, outcome)
    - Wrap storage backend operations (read, write, delete) with OTel spans
    - _Requirements: 9.4, 9.5_

  - [x] 12.4 Replace structlog and Sentry with OTel logging
    - Remove `structlog` imports and configuration across all modules
    - Replace `LOGGER.debug/info/warning` calls with Python stdlib `logging` (OTel-instrumented)
    - Remove Sentry SDK integration; use OTel exception recording on spans instead
    - _Requirements: 9.6, 9.7_

  - [ ]* 12.5 Write property tests for observability
    - **Property 13: OpenTelemetry Span Emission**
    - **Validates: Requirements 9.3, 9.4, 9.5**
    - **Property 14: Log-Trace Correlation**
    - **Validates: Requirements 9.6**

- [x] 13. Modern Redis integration
  - [x] 13.1 Replace django-redis and sync redis usage
    - Update Django cache configuration to use `django.core.cache.backends.redis.RedisCache`
    - Replace any direct `redis` sync client usage with `redis.asyncio` from redis-py 5.x
    - Ensure all Redis usage (cache, event bus, arq) goes through `redis.asyncio`
    - Verify Dragonfly compatibility by avoiding Redis-module-specific commands
    - _Requirements: 10.1, 10.3, 10.4, 10.5, 10.6_

  - [ ]* 13.2 Write property test for async cache
    - **Property 15: Cache Round-Trip**
    - **Validates: Requirements 10.4**

- [x] 14. Containerization and deployment
  - [x] 14.1 Create modern Dockerfile
    - Multi-stage build using Python 3.12 slim base
    - Install dependencies via `uv`
    - Entrypoint: `uvicorn p2.core.asgi:application` with configurable host/port
    - Run migrations on startup before accepting traffic
    - _Requirements: 14.1, 14.5_

  - [x] 14.2 Create docker-compose.yml for local development
    - Services: PostgreSQL, Redis (or Dragonfly), p2-web (uvicorn), p2-worker (arq)
    - Volume mounts for local development
    - Environment variable configuration
    - _Requirements: 14.2, 14.4_

  - [x] 14.3 Update Helm chart templates
    - Update deployment manifests for uvicorn entrypoint instead of uwsgi
    - Update worker deployment for arq instead of Celery
    - Update configmap for new environment variables
    - Target Kubernetes 1.28+ API versions
    - _Requirements: 14.3_

  - [x] 14.4 Create .env.example with documented configuration
    - Document all environment variables: database URL, Redis URL, OTEL endpoint, OIDC settings, Fernet key, etc.
    - _Requirements: 14.4_

- [x] 15. Cleanup and final wiring
  - [x] 15.1 Remove deprecated code and dependencies
    - Remove `django-guardian` from installed apps and all `guardian` imports
    - Remove `PublicAccessController` component (replaced by `Volume.public_read`)
    - Remove `p2/core/celery.py` and all Celery-related imports
    - Remove `signal_marshall` task and Django signal receivers for `BLOB_POST_SAVE` / `BLOB_PAYLOAD_UPDATED`
    - Remove `drf-yasg`, `djangorestframework-jwt`, `mozilla-django-oidc` imports
    - Remove `structlog` imports and configuration
    - Remove `django-prometheus` / `ExportModelOperationsMixin` references
    - Remove unused hash constants (`SHA1`, `SHA384`, `SHA512`)
    - _Requirements: 1.3, 1.6, 1.7, 5.1, 7.3, 9.6, 9.8_

  - [x] 15.2 Wire event consumers and arq worker into application startup
    - Ensure event consumers start when the arq worker process launches
    - Ensure `setup_telemetry()` is called in both ASGI app and worker process
    - Verify all component handlers are registered as event consumers
    - _Requirements: 4.1, 8.1, 8.2, 9.1_

- [x] 16. Final checkpoint - Full integration
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The `BLOB_PRE_SAVE` quota check remains synchronous by design — it must block writes
