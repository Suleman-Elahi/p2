# p2

A petabyte-scale, S3-compatible object storage server built on Django 5 with a hybrid Python/Rust architecture. p2 delivers MinIO-level I/O performance while keeping the developer ergonomics of the Django ecosystem.

```
Clients (AWS CLI / boto3 / SDKs / Browser)
         │
         ▼
   Nginx  ──────────────────────────────────────────────────────────┐
   (reverse proxy)                                                   │ sendfile()
         │                                                           │ zero-copy
         ▼                                                           ▼
   Uvicorn + uvloop (ASGI)                              /internal-storage/…
   ├── S3 API  (AWS v4 auth via Rust HMAC)
   ├── REST API (DRF + JWT)
   └── Web UI  (Django templates)
         │
         ├── Auth check  ──► Turso / SQLite (libSQL embedded replica, ~0.1 ms)
         ├── Metadata     ──► redb Rust engine  (sub-ms prefix scans, 1B+ keys)
         └── Blob I/O     ──► aiofiles  4 MB chunks  +  Rust inline checksums
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

---

## Architecture

### Control Plane — Turso / libSQL

Users, API keys, volumes (buckets), ACLs, and serve rules live in a libSQL database. In production this is a **Turso embedded replica** — the full database is synced to a local file so every auth check reads from disk at ~0.1 ms with zero network round-trips. In local dev it falls back to plain SQLite automatically.

### Metadata Engine — Rust + redb

Every blob's metadata (path, size, MIME, ETag, timestamps) is stored in a per-volume **redb** database via a PyO3 Rust extension (`p2_s3_meta`). redb is an embedded key-value store with MVCC transactions and memory-mapped I/O. Prefix scans for `ListObjectsV2` run entirely in Rust, bypassing Python's GIL, and handle 1 billion+ keys without breaking a sweat.

### Zero-Copy Reads — Nginx X-Accel-Redirect

For `GET Object` requests Django authenticates the request and looks up metadata (~1 ms total Python time), then returns an empty response with an `X-Accel-Redirect` header pointing at the physical file path. Nginx intercepts this header and uses the Linux kernel's `sendfile()` syscall to stream the file directly from NVMe to the network socket — Python's memory stays empty for the entire transfer.

### High-Throughput Writes — aiofiles + Rust checksums

`PUT Object` streams the request body through a 4 MB write buffer to `aiofiles`, computing MD5 and SHA-256 inline on every chunk using Python's `hashlib` (with the Rust `p2_s3_checksum` extension available for AWS SDK checksum headers). Once the file is flushed, metadata is committed to redb in a single transaction.

### Event Bus — Dragonfly + arq

After every write, p2 publishes a `blob_post_save` event to a Dragonfly Redis Stream. The `arq` worker picks it up and runs background tasks: EXIF extraction, expiry scheduling, replication, and multipart assembly. Dragonfly is a drop-in Redis replacement that uses all CPU cores, giving far higher stream throughput than single-threaded Redis.

---

## Rust Extensions

Three compiled PyO3 extensions ship as pre-built `.so` files — no Rust toolchain needed to run p2.

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

Verifies `x-amz-checksum-*` headers sent by modern AWS SDKs (CRC32, CRC32C, SHA-256, SHA-1). All algorithms run at native speed with no GIL involvement.

| Function | Description |
|---|---|
| `verify_crc32(data, expected_b64)` | Verify CRC32 |
| `verify_crc32c(data, expected_b64)` | Verify CRC32C |
| `verify_sha256(data, expected_hex)` | Verify SHA-256 |
| `verify_sha1(data, expected_b64)` | Verify SHA-1 |
| `compute_*` variants | Compute each algorithm |

Falls back to Python `hashlib` / `binascii` if absent.

### `p2_s3_meta` — redb metadata engine

Source: `p2/s3/meta_ext/`

The core metadata store. Each volume gets its own redb database at `/storage/volumes/<uuid>/metadata.redb`. A process-level singleton cache (`p2/s3/engine.py`) ensures only one `MetaEngine` instance per database (redb holds an exclusive file lock).

| Method | Description |
|---|---|
| `engine.put(path, json)` | Write or overwrite blob metadata |
| `engine.get(path)` | O(1) point lookup |
| `engine.list(prefix, start_after, max_keys)` | Range scan for ListObjectsV2 |
| `engine.delete(path)` | Remove a key |

### Building the extensions

The compiled `.so` files are committed to the repo. Rebuild only when you change the Rust source.

```bash
# Install Rust (if not already installed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install maturin
pip install maturin

# Build p2_s3_crypto
cd p2/s3/rust_ext
maturin build --release
cp target/wheels/*.whl ../
cd -

# Build p2_s3_checksum
cd p2/s3/checksum_ext
maturin build --release
cp target/wheels/*.whl ../
cd -

# Build p2_s3_meta
cd p2/s3/meta_ext
maturin build --release
cp target/wheels/*.whl ../
# Install into the venv
pip install target/wheels/p2_s3_meta-*.whl --force-reinstall
cd -

# Commit the updated .so files
git add p2/s3/p2_s3_crypto.so p2/s3/p2_s3_checksum.so p2/s3/p2_s3_meta.so
git commit -m "chore: rebuild Rust extensions"
```

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
- `web` — Uvicorn + uvloop on port 8000
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

# Start the web server
uvicorn p2.core.asgi:application --reload --loop uvloop

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
| `P2_STORAGE__USE_X_ACCEL_REDIRECT` | `false` | Set `true` when Nginx handles downloads |
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

Set in `.env`:
```dotenv
P2_STORAGE__USE_X_ACCEL_REDIRECT=true
```

Add to your Nginx config (see `deploy/nginx.conf` for the full example):
```nginx
location /internal-storage/ {
    internal;
    alias /storage/;
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
}
```

With this enabled, Python handles auth + metadata (~1 ms) and Nginx streams the file bytes at line rate.

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

Or add to `~/.aws/credentials`:
```ini
[p2]
aws_access_key_id = MYAPIKEY
aws_secret_access_key = MYAPISECRET

[profile p2]
region = us-east-1
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

# Check if a bucket is public or private
aws s3api get-bucket-acl $EP --bucket my-bucket
# Public bucket has a grant with URI: http://acs.amazonaws.com/groups/global/AllUsers
# Private bucket only has the owner with FULL_CONTROL

# Check bucket policy
aws s3api get-bucket-policy $EP --bucket my-bucket
# Returns the JSON policy, or NoSuchBucketPolicy error if none set

# Make a single file publicly accessible on a private bucket
aws s3api put-bucket-policy $EP --bucket my-bucket \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::my-bucket/public-file.mp3"
    }]
  }'
# Now http://localhost:8000/my-bucket/public-file.mp3 works anonymously
# Everything else in the bucket stays private

# Make an entire prefix public
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
  --lifecycle-configuration '{
    "Rules":[{
      "ID":"expire-old","Status":"Enabled",
      "Expiration":{"Days":30},
      "Filter":{"Prefix":"tmp/"}
    }]
  }'
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
