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

## tier0 / Serve Rules

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

## License

MIT
