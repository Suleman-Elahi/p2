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

## API

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
