# Requirements Document

## Introduction

p2 is a 7-year-old archived S3-compatible object storage system built on Django 2.2. This modernization effort revives the project to make it performant for internal development and lite production use cases, targeting parity with MinIO and SeaweedFS for those workloads. The scope covers upgrading the framework and all dependencies, replacing synchronous and legacy subsystems with async-native alternatives, simplifying the permission model, optimizing hot-path data operations, and adopting modern observability and auth standards.

## Glossary

- **P2_System**: The p2 object storage application as a whole, including all Django apps, background workers, and gRPC services
- **S3_API_Layer**: The S3-compatible HTTP API (`p2/s3/`) that handles bucket and object operations using AWS v4 signature authentication
- **Core_Engine**: The central storage engine (`p2/core/`) containing Volume, Blob, Storage, and Component models
- **Component_System**: The pluggable feature modules (`p2/components/`) that hook into the blob lifecycle via signals (Quota, Expiry, Replication, Public Access, Image)
- **Signal_Chain**: The sequence of Django signals (`BLOB_PRE_SAVE`, `BLOB_POST_SAVE`, `BLOB_PAYLOAD_UPDATED`) that trigger component logic during blob operations
- **Task_Queue**: The background task processing system currently implemented with Celery and Redis
- **Event_Bus**: The pub/sub mechanism used to dispatch blob lifecycle events to component handlers
- **Storage_Backend**: A pluggable storage controller (Local filesystem or S3-compatible) that handles blob binary data persistence
- **ACL_Model**: The access control system that governs permissions on Volumes and Blobs
- **Serve_Layer**: The gRPC service (`p2/serve/`) that maps URL patterns to blob lookups for web serving
- **Observability_Stack**: The logging, metrics, and tracing infrastructure used for monitoring and debugging
- **Auth_Provider**: The external identity provider integration for single sign-on (currently mozilla-django-oidc)
- **REST_API**: The Django REST Framework management API (`p2/api/`) with JWT authentication
- **Quota_Controller**: The component that enforces maximum storage size on a volume by checking blob size before save
- **Space_Used_Query**: The database query that computes total storage consumption for a volume by aggregating blob size attributes

## Requirements

### Requirement 1: Upgrade Django and Core Dependencies

**User Story:** As a developer, I want p2 to run on Django 5.x with current stable dependencies, so that the project benefits from security patches, performance improvements, and modern Python features.

#### Acceptance Criteria

1. THE P2_System SHALL run on Django 5.x (latest stable release) with Python 3.12 or later
2. THE P2_System SHALL use Django REST Framework 3.15 or later for the REST_API
3. THE P2_System SHALL replace the deprecated `django.contrib.postgres.fields.JSONField` with `django.db.models.JSONField`
4. THE P2_System SHALL replace `psycopg2` with `psycopg[binary]` version 3.x (async-capable driver)
5. THE P2_System SHALL replace `Pillow==5.2.0` with the latest stable Pillow release
6. THE P2_System SHALL replace `djangorestframework-jwt` with `djangorestframework-simplejwt` for JWT authentication
7. THE P2_System SHALL replace `drf-yasg` with `drf-spectacular` for OpenAPI schema generation
8. THE P2_System SHALL replace `Pipfile` with `pyproject.toml` using `uv` as the package manager
9. THE P2_System SHALL replace `uwsgi` with `uvicorn` using `uvloop` as the event loop
10. IF a dependency has no maintained replacement, THEN THE P2_System SHALL document the gap and pin to the last known-good version

### Requirement 2: Async S3 API Layer

**User Story:** As a developer, I want the S3 API views to be fully async, so that p2 can handle high concurrency without blocking on I/O operations.

#### Acceptance Criteria

1. THE S3_API_Layer SHALL use async Django views (async `def get`, `async def put`, etc.) for all bucket and object operations
2. THE S3_API_Layer SHALL use async database queries (`Blob.objects.filter(...).afirst()`, `aexists()`, etc.) for all ORM operations in the request path
3. THE S3_API_Layer SHALL stream request bodies and response bodies asynchronously for object GET and PUT operations
4. WHEN a multipart upload complete request is received, THE S3_API_Layer SHALL assemble parts asynchronously without blocking the event loop
5. THE S3_API_Layer SHALL perform AWS v4 signature verification without blocking the event loop
6. IF an async view calls a synchronous dependency, THEN THE S3_API_Layer SHALL wrap the call in `sync_to_async` with a documented justification

### Requirement 3: Replace Celery with Async Task Queue

**User Story:** As a developer, I want to replace Celery with a lightweight async task queue, so that background tasks run with lower overhead and integrate naturally with the async Django runtime.

#### Acceptance Criteria

1. THE Task_Queue SHALL use `arq` (or equivalent async-native task queue backed by Redis) instead of Celery
2. THE Task_Queue SHALL support delayed task execution (equivalent to Celery's `apply_async(eta=...)` and `apply_async(countdown=...)`)
3. THE Task_Queue SHALL support periodic task scheduling (equivalent to Celery beat) for the expiry sweep task
4. THE Task_Queue SHALL process replication tasks (`replicate_metadata_update_task`, `replicate_payload_update_task`, `replicate_delete_task`) asynchronously
5. THE Task_Queue SHALL process the `signal_marshall` task for dispatching `BLOB_PAYLOAD_UPDATED` events
6. THE Task_Queue SHALL process the `complete_multipart_upload` task for assembling multipart upload parts
7. IF a task fails, THEN THE Task_Queue SHALL retry the task with exponential backoff up to a configurable maximum retry count

### Requirement 4: Replace Django Signals with External Event Bus

**User Story:** As a developer, I want blob lifecycle events dispatched via Redis Streams or NATS instead of in-process Django signals, so that event handling is decoupled, observable, and can scale independently of the web process.

#### Acceptance Criteria

1. THE Event_Bus SHALL use Redis Streams (or NATS JetStream) to publish and consume blob lifecycle events (`BLOB_PRE_SAVE`, `BLOB_POST_SAVE`, `BLOB_PAYLOAD_UPDATED`)
2. THE Event_Bus SHALL deliver `BLOB_PRE_SAVE` events synchronously within the request path so that Quota_Controller can block writes before commit
3. THE Event_Bus SHALL deliver `BLOB_POST_SAVE` and `BLOB_PAYLOAD_UPDATED` events asynchronously to consumer processes
4. THE Event_Bus SHALL guarantee at-least-once delivery for all async events using consumer group acknowledgment
5. THE Event_Bus SHALL include the blob UUID, volume UUID, event type, and timestamp in every event payload
6. WHEN a consumer process restarts, THE Event_Bus SHALL resume processing from the last acknowledged event position

### Requirement 5: Replace django-guardian with Volume-Level ACLs

**User Story:** As a developer, I want a simpler, faster permission model based on volume-level ACLs instead of per-object permissions, so that permission checks do not require per-blob database lookups.

#### Acceptance Criteria

1. THE ACL_Model SHALL enforce permissions at the Volume level (read, write, delete, list, admin) instead of per-Blob object permissions
2. THE ACL_Model SHALL store ACL entries in a dedicated database table mapping (user_or_group, volume, permission_set)
3. THE ACL_Model SHALL support both user-level and group-level ACL entries
4. THE ACL_Model SHALL evaluate permissions using a single indexed query per request instead of per-object guardian lookups
5. THE ACL_Model SHALL support a public-read flag on a Volume that grants anonymous read access to all blobs in that volume, replacing the PublicAccessController component
6. WHEN a volume has no explicit ACL entries for a user, THE ACL_Model SHALL deny access by default
7. THE ACL_Model SHALL provide a data migration path from existing django-guardian object permissions to volume-level ACLs

### Requirement 6: Optimize Space Used Calculation

**User Story:** As a developer, I want volume space usage tracked via a counter column instead of an aggregate query, so that quota checks are O(1) instead of scanning all blobs.

#### Acceptance Criteria

1. THE Core_Engine SHALL maintain a `space_used_bytes` counter column on the Volume model
2. WHEN a Blob is created or its payload is updated, THE Core_Engine SHALL atomically increment the Volume `space_used_bytes` by the blob size using `F()` expressions
3. WHEN a Blob is deleted, THE Core_Engine SHALL atomically decrement the Volume `space_used_bytes` by the blob size using `F()` expressions
4. THE Quota_Controller SHALL read `volume.space_used_bytes` directly instead of running an aggregate query
5. THE Core_Engine SHALL provide a management command that recalculates `space_used_bytes` from actual blob sizes for consistency repair

### Requirement 7: Reduce Hash Computation

**User Story:** As a developer, I want to compute only MD5 (for S3 ETags) and SHA256 (for AWS v4 auth) on blob payload updates, so that hash computation does not waste CPU on unused digests.

#### Acceptance Criteria

1. WHEN a blob payload is updated, THE Core_Engine SHALL compute only MD5 and SHA256 hashes instead of MD5, SHA1, SHA256, SHA384, and SHA512
2. THE Core_Engine SHALL store the MD5 hash in `blob.p2.io/hash/md5` and the SHA256 hash in `blob.p2.io/hash/sha256` as blob attributes
3. THE Core_Engine SHALL remove the constants and attribute keys for SHA1, SHA384, and SHA512 hashes
4. THE Core_Engine SHALL compute hashes using async streaming reads when the storage backend supports async I/O

### Requirement 8: Async Component Signal Chain

**User Story:** As a developer, I want the component signal chain to be async where possible, so that post-save and payload-updated handlers do not block the request path.

#### Acceptance Criteria

1. THE Component_System SHALL execute `BLOB_POST_SAVE` handlers (replication metadata, expiry scheduling, public access) asynchronously via the Event_Bus
2. THE Component_System SHALL execute `BLOB_PAYLOAD_UPDATED` handlers (replication payload, image EXIF, hash computation) asynchronously via the Event_Bus
3. THE Component_System SHALL execute `BLOB_PRE_SAVE` handlers (quota check) synchronously within the request transaction so that writes can be blocked
4. WHEN an async handler fails, THE Component_System SHALL log the failure with full context and retry via the Task_Queue

### Requirement 9: Replace Observability Stack with OpenTelemetry

**User Story:** As a developer, I want structured logging, metrics, and distributed tracing via OpenTelemetry, so that p2 uses a vendor-neutral observability standard instead of structlog+Sentry+Prometheus.

#### Acceptance Criteria

1. THE Observability_Stack SHALL use OpenTelemetry SDK for Python to emit traces, metrics, and logs
2. THE Observability_Stack SHALL export traces and metrics via OTLP (OpenTelemetry Protocol) to a configurable collector endpoint
3. THE Observability_Stack SHALL instrument all S3 API requests with trace spans including method, bucket, key, status code, and latency
4. THE Observability_Stack SHALL instrument all Task_Queue jobs with trace spans including task name, duration, and outcome
5. THE Observability_Stack SHALL instrument all Storage_Backend operations (read, write, delete) with trace spans
6. THE Observability_Stack SHALL replace structlog with OpenTelemetry-compatible structured logging that correlates logs with trace IDs
7. THE Observability_Stack SHALL replace Sentry error reporting with OpenTelemetry exception recording on spans
8. THE Observability_Stack SHALL replace django-prometheus model operation metrics with OpenTelemetry metrics (request count, latency histograms, error rates)

### Requirement 10: Modernize Redis Usage

**User Story:** As a developer, I want p2 to use modern Redis features (or Dragonfly as a drop-in replacement), so that caching, task queuing, and event streaming are efficient and use current client libraries.

#### Acceptance Criteria

1. THE P2_System SHALL use `redis-py` version 5.x with async support (`redis.asyncio`) as the Redis client
2. THE P2_System SHALL use Redis Streams for the Event_Bus implementation
3. THE P2_System SHALL use Redis for the Task_Queue broker (arq's native Redis backend)
4. THE P2_System SHALL use Django's async cache backend with `redis.asyncio` for blob metadata caching
5. THE P2_System SHALL be compatible with Dragonfly as a drop-in Redis replacement without code changes
6. THE P2_System SHALL replace `django-redis` with Django 5.x's built-in Redis cache backend (`django.core.cache.backends.redis.RedisCache`)

### Requirement 11: Modernize gRPC Serve Layer

**User Story:** As a developer, I want the gRPC serve layer to use async gRPC and modern protobuf tooling, so that URL-to-blob serving is non-blocking and maintainable.

#### Acceptance Criteria

1. THE Serve_Layer SHALL use `grpcio` with async server (`grpc.aio`) for non-blocking request handling
2. THE Serve_Layer SHALL use async Django ORM queries for blob lookups and permission checks
3. THE Serve_Layer SHALL stream blob binary data to gRPC responses instead of reading entire blobs into memory
4. THE Serve_Layer SHALL use `grpcio-tools` with `protobuf` version 5.x for proto compilation
5. THE Serve_Layer SHALL replace the `MockRequest` pattern with a proper request context object that carries user identity and trace context

### Requirement 12: Modernize Storage Backends

**User Story:** As a developer, I want storage backends to support async I/O and use modern client libraries, so that blob reads and writes do not block the event loop.

#### Acceptance Criteria

1. THE Storage_Backend SHALL define an async interface with `async def get_read_handle`, `async def commit`, `async def delete`, and `async def collect_attributes` methods
2. THE Storage_Backend local controller SHALL use `aiofiles` for async filesystem I/O
3. THE Storage_Backend S3 controller SHALL use `aiobotocore` (or `s3fs`) instead of synchronous `boto3` for async S3 operations
4. THE Storage_Backend SHALL support streaming reads and writes using async iterators instead of loading entire blobs into memory
5. THE Storage_Backend local controller SHALL replace `python-magic` with `aiofiles`-compatible MIME detection (file extension mapping or async libmagic binding)
6. IF a storage backend operation fails with a transient error, THEN THE Storage_Backend SHALL retry the operation with exponential backoff up to 3 attempts

### Requirement 13: Modern Authentication

**User Story:** As a developer, I want p2 to use a modern, maintained OIDC library and support API key authentication natively, so that auth is secure and does not depend on archived packages.

#### Acceptance Criteria

1. THE Auth_Provider SHALL replace `mozilla-django-oidc` with `authlib` (Django integration) or `python-social-auth` for OIDC/OAuth2 authentication
2. THE Auth_Provider SHALL support OIDC Discovery (`.well-known/openid-configuration`) for automatic endpoint configuration
3. THE Auth_Provider SHALL support PKCE (Proof Key for Code Exchange) for the authorization code flow
4. THE REST_API SHALL use `djangorestframework-simplejwt` for issuing and validating JWT access tokens
5. THE S3_API_Layer SHALL continue to support AWS v4 signature authentication using the existing APIKey model
6. THE P2_System SHALL store API key secrets using a one-way hash and verify by recomputing, instead of storing secrets in plaintext

### Requirement 14: Containerization and Deployment Modernization

**User Story:** As a developer, I want modern container images and deployment manifests, so that p2 is easy to deploy on current Kubernetes clusters and local Docker setups.

#### Acceptance Criteria

1. THE P2_System SHALL provide a multi-stage Dockerfile using Python 3.12 slim base image with uvicorn as the entrypoint
2. THE P2_System SHALL provide a `docker-compose.yml` for local development with PostgreSQL, Redis (or Dragonfly), and the p2 web/worker services
3. THE P2_System SHALL provide updated Helm chart templates compatible with Kubernetes 1.28 or later
4. THE P2_System SHALL support configuration via environment variables with a documented `.env.example` file
5. THE P2_System SHALL run database migrations automatically on container startup before accepting traffic
