# p2

An S3-compatible object storage server built on Django 5.x with an async-first architecture.

p2 is designed for simple, fast file sharing and internal storage workloads. It exposes an S3-compatible API so any AWS SDK or CLI works out of the box, and includes a gRPC serve layer for URL-pattern-based blob routing.

## Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.12+, Django 5.x, ASGI (uvicorn + uvloop) |
| Database | PostgreSQL (psycopg 3.x async driver) |
| Task queue | arq (async, Redis-backed) |
| Event bus | Redis Streams |
| Cache | Django built-in Redis cache backend |
| Auth | djangorestframework-simplejwt (JWT), authlib (OIDC/PKCE) |
| API schema | drf-spectacular (OpenAPI 3.x) |
| Observability | OpenTelemetry SDK (traces, metrics, logs via OTLP) |
| Storage backends | Local filesystem (aiofiles), S3-compatible (aiobotocore) |
| gRPC | grpc.aio async server |

## Quick start (Docker Compose)

```bash
cp .env.example .env
# edit .env — set SECRET_KEY, FERNET_KEY, database/Redis credentials

docker compose up
```

The web service runs migrations automatically on startup, then listens on `http://localhost:8000`.

Default login credentials (created by the initial migration):

| Username | Password |
|---|---|
| `admin` | `admin` |

> Change the password immediately after first login.

## Running locally with uv

```bash
# Install dependencies
uv sync

# Run migrations
uv run python manage.py migrate

# Start the ASGI server
uvicorn p2.core.asgi:application --reload

# Start the async worker (separate terminal)
uv run python -m arq p2.core.worker.WorkerSettings
```

## Configuration

All configuration is via environment variables (or a YAML config file read by `p2.lib.config`). See `.env.example` for the full list. Key variables:

| Variable | Description |
|---|---|
| `SECRET_KEY` | Django secret key |
| `P2_FERNET_KEY` | Fernet key for encrypting API key secrets at rest |
| `P2_POSTGRESQL__HOST` | PostgreSQL host |
| `P2_REDIS__HOST` | Redis host (cache + event bus) |
| `ARQ_REDIS_URL` | Redis URL for the arq task queue |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint for traces/metrics |
| `OTEL_SERVICE_NAME` | Service name reported to the collector |
| `P2_OIDC__DISCOVERY_URL` | OIDC provider discovery URL (authlib) |

## Architecture

```
Clients (AWS CLI / boto3 / SDKs / Browser)
        │
        ▼
  ASGI Server (uvicorn + uvloop)
  ├── S3 API (async Django views, AWS v4 auth)
  ├── REST API (DRF + simplejwt)
  └── Admin UI
        │
        ▼
  Core Engine
  ├── Volume  — logical bucket/namespace
  ├── Blob    — object with path, attributes (JSON), tags
  └── Storage — backend config (local or S3)
        │
        ├── Async Local Storage  (aiofiles)
        └── Async S3 Storage     (aiobotocore)
        │
        ▼
  Event Bus (Redis Streams)
  ├── blob_post_save      → replication metadata, expiry scheduling
  └── blob_payload_updated → hash computation, replication payload, EXIF
        │
        ▼
  Async Worker (arq)
  ├── replicate_metadata / replicate_payload / replicate_delete
  ├── complete_multipart
  └── run_expire (cron, every 60s)
        │
        ▼
  Supporting Services
  ├── gRPC Serve layer  — URL-regex → blob routing
  ├── VolumeACL         — volume-level permissions (replaces django-guardian)
  └── OpenTelemetry     — traces, metrics, log correlation
```

## Concepts

**Storage** — a backend instance (local filesystem or S3-compatible). Configured with a `controller_path` and tags holding connection details.

**Volume** — a logical namespace (like an S3 bucket). Backed by one Storage. Has a `space_used_bytes` counter and a `public_read` flag for anonymous access.

**Blob** — an individual object with a path, binary payload, JSON attributes (size, MIME, hashes), and tags.

**Component** — an opt-in feature module attached to a Volume. Available components:
- **Quota** — blocks writes when `space_used_bytes` exceeds a threshold
- **Expiry** — auto-deletes blobs after a Unix timestamp stored in blob tags
- **Replication** — mirrors blobs 1:1 to a target volume
- **Image** — extracts EXIF metadata from image blobs

**tier0 / Serve** — a gRPC service that maps URL patterns (regex) to blob lookups, enabling custom URL schemes for serving files.

## Storage Backends

p2 ships two storage backends, each with a sync and async implementation.

### Local (`LocalStorageController` / `AsyncLocalStorageController`)

Stores blobs on the local filesystem. Files are written to a configurable root path (set via the `storage.root_path` tag, e.g. `/storage/`) and named after the blob's UUID, sharded into two-level subdirectories (`ab/cd/abcd...uuid`) to avoid large flat directories.

- Required tag: `storage.root_path`
- MIME type is detected using `libmagic` on the raw bytes; a text/binary heuristic is also applied
- Reads and writes are plain file I/O (`open()`), or `aiofiles` in the async variant
- Best for: single-node setups, local dev, or when you don't need distributed storage

### S3-compatible (`S3StorageController` / `AsyncS3StorageController`)

Stores blobs in any S3-compatible object store — AWS S3, MinIO, Ceph, etc. The volume name maps to the S3 bucket and the blob path maps to the object key.

- Required tags: `s3.access_key`, `s3.secret_key`, `s3.region`
- Optional tags: `s3.endpoint` (for non-AWS endpoints like MinIO), `s3.endpoint_ssl_verify`
- The async variant (`AsyncS3StorageController`) uses `aiobotocore` and adds exponential-backoff retry on transient 5xx errors
- `collect_attributes` (size, MIME type) is a no-op on the sync version — the async version calls `head_object` instead
- Best for: distributed or cloud deployments where you need durable, scalable object storage

### Choosing a backend

| | Local | S3-compatible |
|---|---|---|
| Setup complexity | None | Requires credentials + bucket |
| Scalability | Single node | Horizontally scalable |
| MIME detection | `libmagic` (accurate) | From `Content-Type` header |
| Async support | `aiofiles` | `aiobotocore` |
| Retry logic | — | Exponential backoff (async) |
| Good for | Dev / single-node | Production / cloud |

Storage instances are configured in the Django admin or via the REST API at `/_/api/v1/core/storage/`.

## S3 API Compatibility

Current estimated compatibility: **~90%** of the S3 API surface that matters for real-world SDK usage.

### Recently completed

The following features were added based on analysis of [hs5](https://github.com/uroni/hs5), a high-performance C++ S3 server:

| Feature | Type | Description |
|---|---|---|
| IAM-style bucket policies | Python | `GET/PUT/DELETE /<bucket>?policy` — deny-overrides-allow evaluation, wildcard resource matching |
| UploadPartCopy | Python | `PUT /<bucket>/<key>?uploadId&partNumber` with `x-amz-copy-source` — cross-volume supported |
| Payload checksum verification | Rust ext | CRC32, CRC32C, SHA-256, SHA-1 via `x-amz-checksum-*` headers |
| Conditional headers on PUT/Copy | Python | `If-Match`, `If-None-Match`, `If-Unmodified-Since` → 412 Precondition Failed |
| Conditional headers on GET | Python | `If-None-Match`, `If-Modified-Since` → 304 Not Modified |
| ETag in responses | Python | GET, HEAD, PUT now return `ETag` header |
| S3 Lifecycle API | Python | `GET/PUT/DELETE /<bucket>?lifecycle` — exposes existing Expiry component |
| GetBucketNotification stub | Python | Returns empty `<NotificationConfiguration/>` — stops SDK warnings |
| MD5 in Rust | Rust ext | `md5_hex()` / `md5_bytes()` added to `p2_s3_crypto` for fast ETag computation |

### Service-level operations

| Operation | Status | Notes |
|---|---|---|
| `GET /` — ListBuckets | ✅ | Async, returns volumes the user has ACL access to |

### Bucket operations

| Operation | Status | Notes |
|---|---|---|
| `PUT /<bucket>` — CreateBucket | ✅ | Creates Volume + VolumeACL |
| `DELETE /<bucket>` — DeleteBucket | ✅ | |
| `HEAD /<bucket>` — HeadBucket | ✅ | |
| `GET /<bucket>` — ListObjectsV2 | ✅ | prefix, delimiter, max-keys, continuation-token, start-after |
| `GET /<bucket>?location` — GetBucketLocation | ✅ | Returns us-east-1 |
| `GET /<bucket>?uploads` — ListMultipartUploads | ✅ | Lists active incomplete uploads |
| `POST /<bucket>?delete` — DeleteObjects | ✅ | Multi-object delete |
| `PUT /<bucket>?acl` — PutBucketAcl | ✅ | Canned ACLs only |
| `GET /<bucket>?acl` — GetBucketAcl | ✅ | |
| `GET /<bucket>?cors` — GetBucketCors | ✅ | Rules stored in volume tags |
| `PUT /<bucket>?cors` — PutBucketCors | ✅ | |
| `DELETE /<bucket>?cors` — DeleteBucketCors | ✅ | |
| `GET /<bucket>?tagging` — GetBucketTagging | ✅ | Stored under `s3.user/` prefix in volume tags |
| `PUT /<bucket>?tagging` — PutBucketTagging | ✅ | |
| `DELETE /<bucket>?tagging` — DeleteBucketTagging | ✅ | |
| `GET /<bucket>?policy` — GetBucketPolicy | ✅ | IAM-style JSON policies, deny-overrides-allow evaluation |
| `PUT /<bucket>?policy` — PutBucketPolicy | ✅ | Validates IAM policy JSON, stores in volume tags |
| `DELETE /<bucket>?policy` — DeleteBucketPolicy | ✅ | |
| `GET /<bucket>?lifecycle` — GetBucketLifecycle | ✅ | Expiry rules stored in volume tags |
| `PUT /<bucket>?lifecycle` — PutBucketLifecycle | ✅ | |
| `DELETE /<bucket>?lifecycle` — DeleteBucketLifecycle | ✅ | |
| `GET /<bucket>?notification` — GetBucketNotification | ✅ | Stub — returns empty config (SDKs expect this) |
| `GET /<bucket>?versioning` — GetBucketVersioning | ⚠️ | Stub — always returns Disabled |
| `PUT /<bucket>?versioning` — PutBucketVersioning | ❌ | Not implemented |
| `GET /<bucket>?encryption` — GetBucketEncryption | ⚠️ | Returns 404 NoSuchEncryptionConfig — SDKs treat this as "no SSE, proceed" |
| `GET /<bucket>?replication` — GetBucketReplication | ❌ | p2 has replication component but no S3 replication API |
| `GET /<bucket>?object-lock` — GetObjectLockConfiguration | ❌ | |

### Object operations

| Operation | Status | Notes |
|---|---|---|
| `GET /<bucket>/<key>` — GetObject | ✅ | Streaming, async |
| `PUT /<bucket>/<key>` — PutObject | ✅ | Async, quota check via signal |
| `DELETE /<bucket>/<key>` — DeleteObject | ✅ | |
| `HEAD /<bucket>/<key>` — HeadObject | ✅ | |
| `PUT /<bucket>/<key>` with `x-amz-copy-source` — CopyObject | ✅ | Cross-volume supported |
| `GET /<bucket>/<key>?tagging` — GetObjectTagging | ✅ | Stored under `s3.user/` prefix in blob tags |
| `PUT /<bucket>/<key>?tagging` — PutObjectTagging | ✅ | |
| `DELETE /<bucket>/<key>?tagging` — DeleteObjectTagging | ✅ | |
| `GET /<bucket>/<key>?acl` — GetObjectAcl | ✅ | Canned ACLs only |
| `PUT /<bucket>/<key>?acl` — PutObjectAcl | ✅ | Canned ACLs only |
| `OPTIONS /<bucket>/<key>` — CORS preflight | ✅ | |
| `GET /<bucket>/<key>?versionId` — GetObject (versioned) | ❌ | No versioning |
| `DELETE /<bucket>/<key>?versionId` — DeleteObject (versioned) | ❌ | |
| `GET /<bucket>/<key>?torrent` — GetObjectTorrent | ❌ | |
| `PUT /<bucket>/<key>` — PutObject with SSE headers | ❌ | No server-side encryption |
| `GET /<bucket>/<key>` — Range requests (`Range: bytes=`) | ✅ | RFC 7233, memoryview slice, 206 Partial Content |
| `RESTORE /<bucket>/<key>` — RestoreObject | ❌ | |
| `SELECT /<bucket>/<key>` — SelectObjectContent | ❌ | |

### Multipart upload

| Operation | Status | Notes |
|---|---|---|
| `POST /<bucket>/<key>?uploads` — CreateMultipartUpload | ✅ | |
| `PUT /<bucket>/<key>?uploadId&partNumber` — UploadPart | ✅ | |
| `POST /<bucket>/<key>?uploadId` — CompleteMultipartUpload | ✅ | Assembled async via arq worker |
| `DELETE /<bucket>/<key>?uploadId` — AbortMultipartUpload | ✅ | Deletes all part blobs immediately |
| `GET /<bucket>/<key>?uploadId` — ListParts | ✅ | max-parts, part-number-marker supported |
| `PUT /<bucket>/<key>?uploadId&partNumber` with copy source — UploadPartCopy | ✅ | Cross-volume supported |
### Authentication & access

| Feature | Status | Notes |
|---|---|---|
| AWS Signature v4 (header-based) | ✅ | Rust HMAC extension when built |
| AWS Signature v4 (query string / presigned) | ✅ | boto3 `generate_presigned_url()` works |
| Presigned URLs (p2-native) | ✅ | Via `POST /_/api/v1/s3/presign/` |
| AWS v4 presigned URLs (standard format) | ✅ | Validated via existing AWS v4 auth path |
| Virtual-hosted-style URLs (`bucket.s3.example.com`) | ✅ | Via S3RoutingMiddleware |
| Path-style URLs (`/bucket/key`) | ✅ | |
| Anonymous / public-read access | ✅ | Via `volume.public_read` flag |
| IAM-style bucket policies | ✅ | JSON policies stored in volume tags, deny-overrides-allow evaluation |
| STS / temporary credentials | ❌ | |

### What this means in practice

Common tools and their expected compatibility:

| Tool | Works? | Caveats |
|---|---|---|
| `aws s3 cp` | ✅ | |
| `aws s3 sync` | ✅ | Multi-delete, copy, and UploadPartCopy supported |
| `aws s3 ls` | ✅ | Large buckets paginate correctly |
| `aws s3 mb` / `rb` | ✅ | |
| `aws s3api get-object` | ✅ | ETag, conditional headers, range requests |
| `aws s3api put-object-tagging` | ✅ | |
| `aws s3api get-bucket-cors` | ✅ | |
| `aws s3api put-bucket-policy` | ✅ | IAM-style JSON policies |
| `aws s3api put-bucket-lifecycle-configuration` | ✅ | Expiry rules via S3 lifecycle API |
| `boto3` basic CRUD | ✅ | |
| `boto3` presigned URLs | ✅ | boto3 `generate_presigned_url()` works via AWS v4 query-string auth |
| `rclone` | ✅ | Basic ops, policies, lifecycle all work; versioning still unsupported |
| `s3fs` / `goofys` | ✅ | Range requests, conditional headers supported |
| Terraform S3 backend | ⚠️ | Needs versioning + locking |
| Browser direct upload (presigned PUT) | ✅ | Via p2 presign API + CORS |

### What's needed to reach ~95% compatibility

Remaining gaps:

1. Object versioning — large effort, requires schema change
2. Server-side encryption (SSE-S3 / SSE-KMS)
3. Object locking / WORM (depends on versioning)
4. `SelectObjectContent` — S3 Select SQL queries
5. STS / temporary credentials
6. `PutBucketNotification` — p2 has Redis Streams events internally but no S3 notification API

### Rust HMAC extension

The AWS v4 HMAC key derivation runs on every authenticated request. A PyO3 Rust extension (`p2/s3/rust_ext/`) provides ~10x faster signing when built. It also includes `md5_hex()` / `md5_bytes()` for fast ETag computation. The compiled `.so` is committed to the repo so Docker never needs a Rust toolchain.

**First-time setup or after changing `p2/s3/rust_ext/`:**

```bash
# The script auto-installs Rust and maturin if missing.
# Supports Debian/Ubuntu, Arch Linux, and macOS.
./scripts/build_rust_ext.sh

# Commit the compiled extension so everyone else gets it automatically
git add p2/s3/p2_s3_crypto.so
git commit -m "chore: update compiled Rust HMAC extension"
```

**Everyone else (no Rust needed):**

```bash
docker compose up
```

The extension is optional — p2 falls back to Python `hmac` automatically if `p2_s3_crypto.so` is absent, so the app works fine without it.

### Rust checksum extension

A second PyO3 extension (`p2/s3/checksum_ext/`) provides fast CRC32, CRC32C, SHA-256, and SHA-1 payload checksum verification. Modern AWS SDKs send `x-amz-checksum-*` headers; this extension validates them at native speed.

```bash
./p2/s3/checksum_ext/build.sh
git add p2/s3/p2_s3_checksum.so
git commit -m "chore: update compiled Rust checksum extension"
```

Like the HMAC extension, this is optional — p2 falls back to Python `hashlib` / `binascii` if the `.so` is absent.



tier0 is p2's URL-routing layer. A **ServeRule** maps an incoming request (matched by regex against the URL path, hostname, or any HTTP header) to a blob lookup query. The gRPC `Serve` service iterates all rules in order, finds the first match, resolves the blob, checks read permissions, and returns the file data.

This lets you serve blobs at arbitrary URLs without exposing the internal `/_/ui/` paths — useful for CDN-style file serving, per-host asset routing, or custom download URLs.

### How a rule works

Each rule has two parts:

**1. Match tags** — one or more key/value pairs where the value is a regex. All tags must match for the rule to trigger.

| Tag key | Matches against |
|---|---|
| `serve.p2.io/match/path` | Full request path, e.g. `/images/logo.png` |
| `serve.p2.io/match/path/relative` | Path without leading slash, e.g. `images/logo.png` |
| `serve.p2.io/match/host` | Request `Host` header, e.g. `assets.example.com` |
| `serve.p2.io/match/meta/<KEY>` | Any HTTP header by Django META key, e.g. `serve.p2.io/match/meta/HTTP_USER_AGENT` |

**2. Blob query** — a Django ORM filter string built from `key=value` pairs joined by `&`. The value supports these placeholders:

| Placeholder | Replaced with |
|---|---|
| `{path}` | Full request path (with leading slash) |
| `{path_relative}` | Path without leading slash |
| `{host}` | Request hostname |
| `{match[0]}`, `{match[1]}`, … | Regex capture groups from the match tag |
| `{meta[X]}` | Any request header value |

### Creating a rule

Go to `/_/ui/serve/rule/` and click the `+` button, or use the REST API at `/_/api/v1/tier0/policy/`.

Fill in:
- **Name** — a human-readable label
- **Tags** — the match conditions (key/value pairs)
- **Blob query** — the ORM filter to resolve the blob

Use the debug button (▶) on the rule list to test a path against a rule before going live.

### Examples

**Serve a file by exact path**

Match any request for `/downloads/installer.exe` and return the blob at that path in the `releases` volume:

```
Name:       serve installer
Tags:       serve.p2.io/match/path = ^/downloads/installer\.exe$
Blob query: path=/downloads/installer.exe&volume__name=releases
```

**Serve files from a volume by path prefix**

Capture everything under `/assets/` and look up the blob by the captured sub-path:

```
Name:       static assets
Tags:       serve.p2.io/match/path = ^/assets/(.+)$
Blob query: path=/{match[1]}&volume__name=assets
```

A request to `/assets/css/main.css` resolves to the blob at path `/css/main.css` in the `assets` volume.

**Per-hostname routing**

Route requests to `cdn.example.com` to a dedicated volume:

```
Name:       cdn volume
Tags:       serve.p2.io/match/host  = ^cdn\.example\.com$
            serve.p2.io/match/path  = ^/(.+)$
Blob query: path=/{match[1]}&volume__name=cdn
```

**Match by User-Agent**

Serve a different binary to Windows clients:

```
Name:       windows download
Tags:       serve.p2.io/match/path             = ^/download/app$
            serve.p2.io/match/meta/HTTP_USER_AGENT = .*Windows.*
Blob query: path=/releases/app-windows.exe&volume__name=releases
```

### REST API

```bash
# Create a rule
curl -X POST http://localhost:8000/_/api/v1/tier0/policy/ \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "static assets",
    "tags": {
      "serve.p2.io/match/path": "^/assets/(.+)$"
    },
    "blob_query": "path=/{match[1]}&volume__name=assets"
  }'
```



## S3 CLI usage

Configure an AWS CLI profile pointing at your local p2 instance:

```bash
aws configure --profile p2
# AWS Access Key ID: <your API key>
# AWS Secret Access Key: <your API secret>
# Default region name: us-east-1
# Default output format: json
```

Or add it directly to `~/.aws/credentials` and `~/.aws/config`:

```ini
# ~/.aws/credentials
[p2]
aws_access_key_id = <your-api-key>
aws_secret_access_key = <your-api-secret>

# ~/.aws/config
[profile p2]
region = us-east-1
```

API keys are managed at `/_/ui/api/key/`.

### Common operations

```bash
# List buckets (volumes)
aws s3 ls --profile p2 --endpoint-url http://localhost:8000

# Upload a single file
aws s3 cp README.md s3://my-volume/ --profile p2 --endpoint-url http://localhost:8000

# Upload a directory recursively (exclude common noise)
aws s3 sync . s3://my-volume/ --profile p2 --endpoint-url http://localhost:8000 \
  --exclude ".git/*" \
  --exclude ".venv/*"

# Download a file
aws s3 cp s3://my-volume/README.md ./README.md --profile p2 --endpoint-url http://localhost:8000

# List objects in a bucket
aws s3 ls s3://my-volume/ --profile p2 --endpoint-url http://localhost:8000

# Delete an object
aws s3 rm s3://my-volume/README.md --profile p2 --endpoint-url http://localhost:8000
```

> Note: volumes must have a `VolumeACL` entry for your user before S3 access works.
> New volumes created via the UI automatically get full permissions for the creator.
> For volumes created via migrations or scripts, add the ACL through the Django shell:
> ```python
> from p2.core.models import Volume
> from p2.core.acl import VolumeACL
> from django.contrib.auth.models import User
> user = User.objects.get(username='admin')
> volume = Volume.objects.get(name='my-volume')
> VolumeACL.objects.get_or_create(volume=volume, user=user,
>     defaults={'permissions': ['read', 'write', 'delete', 'list', 'admin']})
> ```

The REST API is available at `/_/api/v1/`. Interactive docs:
- Swagger UI: `/_/api/schema/swagger-ui/`
- ReDoc: `/_/api/schema/redoc/`

JWT tokens: `POST /_/api/auth/token/`

The S3-compatible API is routed via the `S3RoutingMiddleware` — requests with an `X-Amz-Date` header or `X-Amz-Signature` query parameter are handled as S3 requests.

## Deployment (Kubernetes)

Helm chart is in `operator/helm-charts/p2/`. Minimal values:

```yaml
version: <image-tag>
secret_key: "<your-secret-key>"
config:
  fernetKey: "<your-fernet-key>"
  otelEndpoint: "http://otel-collector:4317"
```

```bash
helm install p2 operator/helm-charts/p2/ -f values.yaml
```

The chart deploys a web deployment (uvicorn), a worker deployment (arq), and a gRPC serve deployment.

## gRPC Serve layer

The tier0 gRPC server runs as a separate process on port `50051`. It's included in `docker compose up` as the `grpc` service.

To use it from outside Docker, point your gRPC client at `localhost:50051`. The proto definition is at `p2/grpc/protos/serve.proto`.

## Development

```bash
# Lint
uv run pylint p2/

# Tests
uv run pytest

# Regenerate protobuf stubs
uv run python -m grpc_tools.protoc -I protos \
  --python_out=p2/grpc/protos \
  --grpc_python_out=p2/grpc/protos \
  protos/serve.proto
```

Test results and performance benchmarks are documented in [TESTS.md](TESTS.md).

## License

MIT
