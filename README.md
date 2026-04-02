# p2

A petabyte-scale, S3-compatible object storage server built on Django 5. p2 delivers high-throughput S3-compatible object storage while keeping the developer ergonomics of the Django ecosystem.

```
Clients (AWS CLI / boto3 / SDKs / Browser)
         │
         ▼
   Uvicorn + uvloop  (8 worker processes, ASGI)
   ├── S3 API   (AWS v4 auth via Rust HMAC)
   ├── REST API (DRF + JWT)
   └── Web UI   (Django templates)
         │
         ├── Auth check  ──► Turso / SQLite (libSQL embedded replica, ~0.1 ms)
         ├── Metadata    ──► LMDB  (B+Tree, memory-mapped, multi-process zero-copy, 1B+ keys)
         └── Blob I/O    ──► Tiered Python file serving  +  Rust inline checksums
                              │
                              ▼
                       /storage/volumes/<uuid>/ab/cd/<blob-uuid>
         │
         ▼
   Dragonfly (Redis-compatible, multi-core)
   └── Redis Streams  ──► arq worker
                          ├── EXIF extraction
                          ├── Expiry / lifecycle
                          ├── Replication
                          └── Multipart assembly
```

### Optional: Zero-Copy Nginx Fast Path

When `P2_STORAGE__USE_X_ACCEL_REDIRECT=true` and Nginx is configured as the front-end, file serving bypasses Python entirely:

```
Clients ──► Nginx (reverse proxy) ──► Uvicorn (auth + LMDB metadata only)
                │                               │
                │◄──────── X-Accel-Redirect ────┘
                │
                └──► sendfile() zero-copy ──► Socket
```

---

## Architecture

### Control Plane — Turso / libSQL

Users, API keys, volumes (buckets), ACLs, and serve rules live in a libSQL database. In production this is a **Turso embedded replica** — the full database is synced to a local file so every auth check reads from disk at ~0.1 ms with zero network round-trips. In local dev it falls back to plain SQLite automatically.

### Metadata Engine — LMDB

Every blob's metadata (path, size, MIME, ETag, timestamps) is stored in a per-volume **LMDB** database (`p2/s3/engine.py`). LMDB uses OS-level memory mapping (`mmap`) providing:

- **Multi-process zero-copy reads** — all 8 Uvicorn workers concurrently read the same memory-mapped file with no locking. The kernel page cache is shared across processes.
- **Petabyte scale** — virtual map size is capped at 1 TiB per volume. No physical memory is allocated until keys are actually written.
- **Sub-millisecond prefix scans** — B+Tree range iteration for `ListObjectsV2` without loading all keys into RAM.
- **`max_readers=1024`** — supports 8 workers × concurrent request burst without reader slot exhaustion.

Each volume's metadata lives at `/storage/volumes/<uuid>/metadata.lmdb`.

| Method | Description |
|---|---|
| `engine.put(path, json)` | Write or overwrite blob metadata |
| `engine.get(path)` | O(1) point lookup |
| `engine.list(prefix, start_after, max_keys)` | Range scan for ListObjectsV2 |
| `engine.delete(path)` | Remove a key |

> **Previous:** metadata was stored in a `redb` Rust extension (`p2_s3_meta.so`) which held an exclusive file lock — limiting the server to a single Uvicorn process. LMDB removed this constraint and doubled throughput immediately.

### File Serving — Tiered Python

Without Nginx (default), `GET Object` uses a three-tier strategy based on object size:

| Tier | Size | Strategy | Rationale |
|---|---|---|---|
| 1 | ≤ 64 KiB | Synchronous `open().read()` | Page-cached reads complete in ~1 µs and never block the event loop. Avoids thread-pool overhead. |
| 2 | ≤ 1 MiB | `asyncio.to_thread` | Large enough that blocking the event loop is a real risk. |
| 3 | > 1 MiB | `aiofiles` streaming | Avoids allocating a single large buffer in process memory. |

The OS page cache is shared across all 8 Uvicorn workers. We do **not** duplicate hot-object bytes in Python memory — that would use 8× the RAM versus the kernel's shared page cache.

### Zero-Copy Reads — Nginx X-Accel-Redirect (optional)

Set `P2_STORAGE__USE_X_ACCEL_REDIRECT=true` and deploy Nginx in front. Django handles auth + LMDB lookup (~1 ms total Python time), then returns `X-Accel-Redirect` pointing at the physical file path. Nginx uses Linux `sendfile()` to stream bytes directly from NVMe to the socket — Python memory stays empty for the entire transfer. See `deploy/nginx-host.conf`.

### Multi-Worker Concurrency

Uvicorn runs with `--workers 8`, spawning 8 independent OS processes. Each worker runs its own uvloop event loop. Because Python's GIL is per-process, all 8 workers execute Python code truly in parallel across CPU cores.

- LMDB allows unlimited concurrent readers across all workers with zero locking.
- Auth/volume/ACL/metadata caches are per-worker in-process dicts (TTL-based). Cold misses hit the DB; hot paths are pure dict lookups.
- `--lifespan off` skips the ASGI lifespan probe (Django does not implement it).

### High-Throughput Writes — aiofiles + Rust checksums

`PUT Object` streams the request body through a 4 MB write buffer to `aiofiles`, computing MD5 and SHA-256 inline using `hashlib` (with the Rust `p2_s3_checksum` extension for AWS SDK checksum headers). Once the file is flushed, metadata is committed to LMDB in a single transaction.

### Event Bus — Dragonfly + arq

After every write, p2 publishes a `blob_post_save` event to a Dragonfly Redis Stream. The `arq` worker processes background tasks: EXIF extraction, expiry scheduling, replication, and multipart assembly. Dragonfly is a multi-threaded Redis replacement.

---

## Rust Extensions

Two compiled PyO3 extensions ship as pre-built `.so` files — no Rust toolchain needed to run p2.

### `p2_s3_crypto` — AWS v4 HMAC signing

Source: `p2/s3/rust_ext/`

Provides fast HMAC-SHA256 key derivation for AWS Signature v4 authentication. Runs on every authenticated S3 request.

| Function | Description |
|---|---|
| `derive_signing_key(secret, date, region, service)` | AWS v4 4-step key derivation |
| `hmac_sha256_hex(key, msg)` | HMAC-SHA256 → lowercase hex |
| `hmac_sha256_bytes(key, msg)` | HMAC-SHA256 → raw bytes |
| `md5_hex(data)` | MD5 → hex (ETag computation) |
| `md5_bytes(data)` | MD5 → raw bytes |

Falls back to Python `hmac` / `hashlib` if the `.so` is absent.

### `p2_s3_checksum` — Payload checksum verification

Source: `p2/s3/checksum_ext/`

Verifies `x-amz-checksum-*` headers sent by modern AWS SDKs (CRC32, CRC32C, SHA-256, SHA-1). All algorithms run at native speed.

| Function | Description |
|---|---|
| `verify_crc32(data, expected_b64)` | Verify CRC32 |
| `verify_crc32c(data, expected_b64)` | Verify CRC32C |
| `verify_sha256(data, expected_hex)` | Verify SHA-256 |
| `verify_sha1(data, expected_b64)` | Verify SHA-1 |
| `compute_*` variants | Compute each algorithm |

Falls back to Python `hashlib` / `binascii` if absent.

---

## Quick Start — Docker

```bash
git clone <repo>
cd p2

cp .env.example .env
# Edit .env — at minimum set SECRET_KEY and P2_FERNET_KEY

# Create the storage directory (bind-mounted into containers)
mkdir -p storage/volumes

docker compose up
```

The stack starts:
- `web` — Uvicorn + uvloop, 8 workers, port 8000
- `worker` — arq background worker
- `grpc` — tier0 gRPC serve layer on port 50051
- `redis` — Dragonfly (Redis-compatible) on port 6379

Migrations run automatically before `web` starts.

Default credentials: `admin` / `admin` — change immediately.

Web UI: http://localhost:8000
API docs: http://localhost:8000/_/api/schema/swagger-ui/

---

## Quick Start — Local (uv)

```bash
# Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Copy and edit config
cp .env.example .env
# Set SECRET_KEY, P2_FERNET_KEY, and point REDIS_URL at a local Redis/Dragonfly

# Create storage directory
mkdir -p storage/volumes

# Run migrations
uv run python manage.py migrate

# Start the web server (single worker for local dev)
uvicorn p2.core.asgi:application --reload --loop uvloop --lifespan off

# Start the worker (separate terminal)
uv run python -m arq p2.core.worker.WorkerSettings
```

---

## Configuration

All config is via environment variables. The `P2_` prefix maps to nested YAML keys (e.g. `P2_S3__BASE_DOMAIN` → `s3.base_domain`).

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | — | Django secret key (required) |
| `P2_FERNET_KEY` | — | Fernet key for API secret encryption (required) |
| `P2_DEBUG` | `false` | Enable Django debug mode |
| `P2_LIBSQL__SYNC_URL` | — | Turso sync URL (`libsql://…`). Omit for local SQLite |
| `P2_LIBSQL__AUTH_TOKEN` | — | Turso auth token |
| `P2_LIBSQL__FILE` | `p2-control.db` | Local libSQL database path |
| `REDIS_URL` | `redis://redis:6379/0` | Dragonfly/Redis URL for event streams |
| `ARQ_REDIS_URL` | `redis://redis:6379/1` | arq task queue URL |
| `P2_S3__BASE_DOMAIN` | `s3.example.com` | Base domain for virtual-hosted bucket URLs |
| `P2_STORAGE__USE_X_ACCEL_REDIRECT` | `false` | Set `true` when Nginx handles downloads via X-Accel-Redirect |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP collector endpoint (leave blank to disable) |
| `OTEL_SERVICE_NAME` | `p2` | Service name for traces/metrics |

Generate keys:
```bash
# SECRET_KEY
python -c "from django.utils.crypto import get_random_string; print(get_random_string(50))"

# P2_FERNET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Enabling Turso (Production Control Plane)

```bash
# Install Turso CLI
curl -sSfL https://get.tur.so/install.sh | bash

# Create a database
turso db create p2-control

# Get the sync URL and token
turso db show p2-control --url
turso db tokens create p2-control
```

Add to `.env`:
```dotenv
P2_LIBSQL__SYNC_URL=libsql://p2-control-<org>.turso.io
P2_LIBSQL__AUTH_TOKEN=<token>
P2_LIBSQL__FILE=/storage/control.db
```

---

## Enabling Zero-Copy Downloads (Nginx)

Set `P2_STORAGE__USE_X_ACCEL_REDIRECT=true` in your environment.

The `deploy/nginx-host.conf` file contains the full ready-to-use Nginx configuration including:
- Upstream keepalive pool (512 connections, 100K requests per connection)
- `/internal-storage/` internal location with `sendfile`, `aio threads`, `tcp_nopush`, `directio 512k`
- Static file serving for `/_/static/`

```bash
# Install config (run from project root)
bash scripts/setup.sh
```

For the X-Accel-Redirect path to work, the Nginx worker user must be able to traverse to your storage directory. If Nginx runs as `http` or `www-data` and your storage is inside a home directory:

```bash
chmod o+x /home/<user>          # allow traversal into home dir
chmod -R o+r /path/to/storage/  # ensure files are world-readable
```

With Nginx enabled, Python handles auth + metadata (~1–2 ms) and Nginx serves file bytes at line rate via `sendfile()`.

---

## Performance

Benchmarked with [warp](https://github.com/minio/warp) (`--obj.size=4KiB --concurrent=20 --duration=30s`):

| Config | GET (standalone) | Mixed Total |
|---|---|---|
| Single Uvicorn worker + redb | ~500 obj/s | ~380 obj/s |
| 8 Uvicorn workers + LMDB | ~750 obj/s | ~870 obj/s |
| 8 workers + LMDB + Nginx sendfile | ~1,000+ obj/s (peak 2,000+) | ~1,280 obj/s |

Key architectural decisions for throughput:
- **LMDB over redb**: LMDB allows multiple OS processes to concurrently memory-map the same file. `redb` enforces an exclusive intra-process lock, making multi-worker deployments impossible.
- **8 Uvicorn workers**: Bypasses Python's GIL by running separate OS processes. Auth + metadata is CPU-bound Python; parallelising it scales linearly with cores.
- **Per-process in-memory caches**: Auth, volume, ACL, and metadata lookups are cached in each worker's process memory with TTL expiry. Cache hit = pure dict lookup with zero I/O.
- **Tiered file serving**: Tiny files read synchronously (OS page-cache, ~1 µs), medium files via thread pool, large files via `aiofiles` streaming. The OS page cache is shared across all workers — no per-process byte duplication.

---

## AWS CLI Usage

Configure a profile:
```bash
aws configure --profile p2
# Access Key ID:     <your API key from /_/ui/api/access-key/>
# Secret Access Key: <your API secret>
# Region:            us-east-1
# Output format:     json
```

### Common operations

```bash
EP="--endpoint-url http://localhost:8000 --profile p2"

# List buckets
aws s3 ls $EP

# Create a bucket
aws s3 mb s3://my-bucket $EP

# Upload a file
aws s3 cp ./video.mp4 s3://my-bucket/videos/video.mp4 $EP

# Upload a directory
aws s3 sync ./assets s3://my-bucket/assets $EP --exclude ".git/*"

# Download a file
aws s3 cp s3://my-bucket/videos/video.mp4 ./video.mp4 $EP

# List objects
aws s3 ls s3://my-bucket/ $EP --recursive

# Delete an object
aws s3 rm s3://my-bucket/videos/video.mp4 $EP

# Delete a bucket and all contents
aws s3 rb s3://my-bucket $EP --force

# Multipart upload (automatic for files > 8 MB)
aws s3 cp ./large-file.tar.gz s3://my-bucket/ $EP

# Presigned URL (valid 1 hour)
aws s3 presign s3://my-bucket/videos/video.mp4 $EP --expires-in 3600

# Object tagging
aws s3api put-object-tagging $EP \
  --bucket my-bucket --key videos/video.mp4 \
  --tagging '{"TagSet":[{"Key":"project","Value":"demo"}]}'

# Bucket CORS
aws s3api put-bucket-cors $EP --bucket my-bucket \
  --cors-configuration '{
    "CORSRules":[{
      "AllowedOrigins":["https://app.example.com"],
      "AllowedMethods":["GET","PUT"],
      "AllowedHeaders":["*"],
      "MaxAgeSeconds":3600
    }]
  }'

# Bucket policy — make a prefix public
aws s3api put-bucket-policy $EP --bucket my-bucket \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::my-bucket/public/*"
    }]
  }'

# Remove bucket policy
aws s3api delete-bucket-policy $EP --bucket my-bucket
```

### boto3 example

```python
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:8000",
    aws_access_key_id="MYAPIKEY",
    aws_secret_access_key="MYAPISECRET",
    region_name="us-east-1",
)

# Upload
s3.upload_file("./report.pdf", "my-bucket", "reports/report.pdf")

# Download
s3.download_file("my-bucket", "reports/report.pdf", "./report.pdf")

# Presigned URL
url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "my-bucket", "Key": "reports/report.pdf"},
    ExpiresIn=3600,
)
print(url)

# List objects
paginator = s3.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket="my-bucket", Prefix="reports/"):
    for obj in page.get("Contents", []):
        print(obj["Key"], obj["Size"])
```

---

## Web UI

The built-in UI is available at `/_/ui/`. It provides:

- Volume (bucket) management — create, configure ACLs, set CORS/lifecycle policies
- Blob browser — navigate folders, preview files inline (images, video, audio, PDF, text with syntax highlighting)
- File upload — drag-and-drop single files or entire folders; folder uploads show a single progress bar
- Download as ZIP — stream any folder as a ZIP archive without loading it into memory
- API key management

---

## Components

Components are opt-in feature modules attached to a Volume. Configure them at `/_/ui/core/volume/<uuid>/component/create/`.

| Component | Description |
|---|---|
| **Quota** | Blocks writes when the volume exceeds a configured byte limit |
| **Expiry** | Auto-deletes blobs after a Unix timestamp stored in blob tags |
| **Replication** | Mirrors blobs to a target volume on every write |
| **Image** | Extracts EXIF metadata from image blobs via the `image_exif` consumer |

---

## tier0 — URL Routing Layer

tier0 is a gRPC service (port 50051) that maps incoming URL patterns to blob lookups. A **ServeRule** matches a request by regex against the path, hostname, or any HTTP header, then resolves the blob and streams it back.

```bash
# Create a rule via REST API
curl -X POST http://localhost:8000/_/api/v1/tier0/policy/ \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "static assets",
    "tags": {"serve.p2.io/match/path": "^/assets/(.+)$"},
    "blob_query": "path=/{match[1]}&volume__name=assets"
  }'
```

---

## S3 API Compatibility

~90% of the S3 API surface used by real-world SDKs and tools.

| Category | Implemented |
|---|---|
| Bucket CRUD, ListObjectsV2 | ✅ |
| Object CRUD, CopyObject | ✅ |
| Multipart upload (all 5 operations) | ✅ |
| Range requests (206 Partial Content) | ✅ |
| AWS Signature v4 (header + presigned) | ✅ |
| Virtual-hosted + path-style URLs | ✅ |
| CORS, Tagging, ACLs, Lifecycle | ✅ |
| IAM-style bucket policies | ✅ |
| Payload checksums (CRC32/CRC32C/SHA256/SHA1) | ✅ |
| Versioning, SSE, Object Lock | ❌ |

Works with: `aws s3`, `aws s3api`, `boto3`, `rclone`, `s3fs`, `goofys`.

---

## Development

```bash
# Lint
uv run pylint p2/

# Tests
uv run pytest

# Coverage
uv run coverage run -m pytest && uv run coverage report

# Regenerate protobuf stubs
uv run python -m grpc_tools.protoc -I protos \
  --python_out=p2/grpc/protos \
  --grpc_python_out=p2/grpc/protos \
  protos/serve.proto
```

---

## License

MIT
