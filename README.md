# p2 Storage Engine

> **⚠️ This project is a Proof of Concept / MVP and is a work in progress. No production usage is recommended. Running it straighforward is not guaranted so fix minor config env and other issues yourself or let me know in the discussion**

p2 is a blistering-fast, S3-compatible object storage server built on Python, rigorously accelerated by natively compiled Rust extensions and asynchronous event loops.

It is designed to cleanly handle petabyte-scale metadata via LMDB and process concurrent data payloads reaching thousands of objects per second by sidestepping traditional framework bottlenecks.

## 🌟 Key Features

- **S3 API Compatibility:** Plugs seamlessly into AWS SDKs, MinIO tools (`warp`), Cyberduck, and standard REST tooling.
- **Microsecond Cryptography:** File hash pipelines (MD5, SHA256) are offloaded to `p2_s3_crypto`, a custom Rust implementation that unlocks the Python GIL and streams payloads concurrently.
- **Zero-Copy Reads:** Read operations utilize strictly natively configured Nginx `X-Accel-Redirect` bindings to drop the Python loop entirely and feed object bytes to external networks utilizing kernel-level `sendfile()`.
- **Framework-less Write Bypassing:** Write performance acts identically to compiled Rust backends by using a Raw ASGI protocol Interceptor that steals the active raw byte sequence off the Granian sockets prior to Django initialization.
- **Control-Plane Interface:** Features a modern management interface and fully typed OpenAPI documentation cleanly maintained under Django Ninja.

## 🏗 System Architecture

The p2 engine logically segments into two execution paths to maximize throughput:

**1. The Control Plane (Django Ninja)**
Administrative functions such as Account Provisioning, CORS configuration, UI Views, ACLs, and Multipart uploads are parsed conventionally through Django Ninja leveraging Python asynchronous paradigms and Pydantic validation tools.

**2. The Data Plane (Raw ASGI + Rust)**
Core `PUT` and `GET` S3 transaction traffic goes through a hyper-optimized sub-architecture.

- The raw ASGI application loop inherently recognizes the characteristics of S3 traffic signatures.
- Once matched, S3 `PUT` bytes bypass Django middleware completely. They are pipelined into `p2_s3_crypto` which computes hashes without stalling the application thread, simultaneously streaming payload data physically to your logical drive (`STORAGE`).
- Metadata is asynchronously pushed to `LMDB` inside dedicated queues to retain atomic state tracking while guaranteeing the highest network throughput overhead.

---

## 🚀 Running the Server

### Running with Docker

For the first Docker boot, use the setup script instead of calling `docker compose up` directly. It creates or updates `.env`, prepares storage paths, installs the host nginx config, runs the Docker stack, and recalculates persisted volume stats.

```bash
bash scripts/setup.sh
```

- After the first setup, use normal Docker commands to manage the stack:

```bash
docker compose up -d
docker compose down
docker compose logs -f web
```

- **Web Server:** Binds implicitly to `localhost:8787`.
- **Storage Directory:** Dynamically generates and syncs project-root `./storage/` into the central orchestrator mapped volume path.
- **Included services:** Dragonfly/Redis-compatible broker, web, grpc, migrations, static collection, and background workers.

### Running Natively

For local development, manual benchmarking, or specific UNIX integrations.

Before starting the native flow:

- Copy `.env.example` to `.env` and review the values if you want custom secrets, storage paths, or hostnames.
- Ensure a local Dragonfly or Redis-compatible server is installed and reachable for cache + ARQ. The native script does not provision it for you.
- Ensure nginx can be installed or is already available if you want `X-Accel-Redirect`.

Then use the native bootloader:

```bash
bash scripts/run_without_docker.sh
```

- Creates `.env` from `.env.example` if it is missing.
- Constructs and binds `P2_STORAGE__ROOT` to your project structure.
- Triggers `granian` asynchronously to evaluate socket requests natively without the Docker Network overhead.
- By default, verbose Web UI and `granian` access logs are **disabled** for maximized performance profiling. You can temporarily enable debug tracing by pushing `P2_DEBUG=true` safely into your `.env` manifest before launch.
- **Memory footprint:** ~586 MiB total across 4 Granian workers at idle.

## 🔧 Nginx Configuration (X-Accel-Redirect)

To trigger the `Zero-Copy` streaming architecture for GET operations, `p2` expects you to pair your deployment with Nginx natively forwarding traffic using proxy configurations.

The setup scripts generate the host nginx config for you based on the local storage path and expected Granian upstream.

- You must deploy Nginx directing traffic pointing explicitly to `http://127.0.0.1:8787` (Granian process pipeline).
- Ensure `.env` sets `P2_STORAGE__USE_X_ACCEL_REDIRECT=true`

Once successfully mapped, Granian emits a specialized `0-byte` header interceptor payload commanding the Nginx Daemon daemon to rapidly broadcast and close out file chunks direct from file system IO mapping, achieving near-hardware network saturation limitations!

---

## 📊 Benchmark Results (Native Execution)

Below are the audited `warp` benchmark metrics captured locally bypassing Docker (reflects real-world Nginx/Granian saturation on localhost testing):

- **GET Throughput:** `~8,799 Objects/sec` (Avg: 34.37 MiB/s)
- **PUT Throughput:** `~2,140 Objects/sec` (Avg: 8.36 MiB/s)
