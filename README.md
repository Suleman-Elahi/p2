# p2 Storage Engine

> **⚠️ This project is a Proof of Concept / MVP and is a work in progress. No production usage is recommended. Running it straighforward is not guaranted so fix minor config env and other issues yourself or let me know in the discussion**

p2 (fork of archived https://github.com/BeryJu/p2) is a blistering-fast, S3-compatible object storage server built on Python, rigorously accelerated by natively compiled Rust extensions and asynchronous event loops.

It is designed to cleanly handle petabyte-scale metadata via LMDB and process concurrent data payloads reaching thousands of objects per second by sidestepping traditional framework bottlenecks.

[![Youtube Video](docs/p2_banner.jpg)](https://youtu.be/AFO3vRxm_O4)

## 🌟 Key Features

- **S3 API Compatibility:** Plugs seamlessly into AWS SDKs, MinIO tools (`warp`), Cyberduck, and standard REST tooling.
- **Microsecond Cryptography:** File hash pipelines (MD5, SHA256) are offloaded to `p2_s3_crypto`, a custom Rust implementation that unlocks the Python GIL and streams payloads concurrently.
- **Proxy-Free Reads:** Granian serves object bytes itself. Reads resolve an object's block ranges from LMDB and stream them straight out of the append-only volume files, with no reverse proxy and no `sendfile()` handoff in the path.
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

For the first Docker boot, use the setup script instead of calling `docker compose up` directly. It creates or updates `.env`, prepares storage paths, runs the Docker stack, and recalculates persisted volume stats.

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

Then use the native bootloader:

```bash
bash scripts/run_without_docker.sh
```

- Creates `.env` from `.env.example` if it is missing.
- Constructs and binds `P2_STORAGE__ROOT` to your project structure.
- Triggers `granian` asynchronously to evaluate socket requests natively without the Docker Network overhead.
- No reverse proxy involved — Granian serves the S3 data plane, Django, and the SPA directly on `localhost:8787`. This is now the only deployment topology; Docker uses it too.
- By default, verbose Web UI and `granian` access logs are **disabled** for maximized performance profiling. You can temporarily enable debug tracing by pushing `P2_DEBUG=true` safely into your `.env` manifest before launch.
- **Memory footprint:** ~586 MiB total across 4 Granian workers at idle.

## 🔧 Reverse Proxy

None required. Granian binds `:8787` and serves the S3 data plane, the Django control plane, and the SPA directly.

Earlier versions paired p2 with Nginx and handed object reads off via `X-Accel-Redirect`, so the kernel could `sendfile()` the object straight from disk. That worked when every object was its own file, but it does not fit the append-only design: an object's bytes are now one or more block ranges *inside* a shared volume file, alongside unrelated objects. There is no single file to hand to `sendfile()`, so the redirect had nothing meaningful to point at and has been removed along with the Nginx dependency.

Reads are served by resolving an object's block list from LMDB and streaming those ranges out of the volume files (`p2/s3/volume_reader.py`).

You can of course still put a proxy in front of p2 for TLS termination, routing, or rate limiting — p2 just no longer needs one to serve object bytes.

---

## 📊 Benchmark Results (Native Execution)

`warp`, 4 KiB objects, concurrency 20, 30s, against Granian on `127.0.0.1:8787`:

| | observed range |
|---|---|
| **GET** | 4,900 – 7,100 obj/s (19 – 28 MiB/s), p50 2.5 – 3.9 ms |
| **PUT** | 840 – 2,770 obj/s (3.3 – 10.8 MiB/s), p50 7.3 – 26 ms |

**These are ranges because this setup is not reproducible run to run.** Four consecutive PUT runs on the same machine produced 2,770 / 1,690 / 840 / 1,440 obj/s — a 3.3x spread, with p50 swinging from 7 ms to 350 ms. Treat any single number from this configuration as noise. Known contributors:

- **Worker load imbalance.** Sampling per-worker CPU during a run gave shares of 59% / 17% / 24% / **0%** across the four Granian workers — one worker served most of the traffic while another got none. With only 20 keep-alive connections spread over 4 workers, whichever way connections land at accept time decides the result. The tight 346 ms latency cluster in one run was queueing behind a single saturated worker, not slow code.
- **The load generator shares the box.** `warp` runs on the same 4 cores as 4 Granian workers plus the arq worker and the gRPC server. Client and server compete for CPU. Run `warp` from a second machine before trusting any figure.
- **Throughput degrades as the dataset grows** and partially recovers on server restart, so both on-disk size and per-process state are involved. Not yet diagnosed.
- **Hardware:** Intel i5-6500T (4 cores, 2.5 GHz, 2015), SATA SSD, `btrfs`. Modest.
- **Durability was off** (`P2_S3__VOLUME__FDATASYNC=false`). On this filesystem `fdatasync` costs ~1.5 ms median / ~6.4 ms p99, so enabling it changes PUT latency substantially.
- Earlier figures measured on different hardware with the removed Nginx `X-Accel-Redirect` read path are not comparable.

For context on where PUT time actually goes: a 4 KiB `pwrite` costs ~6 µs here, and profiling shows the io_uring write submission is ~6% of per-request CPU work while auth, the ORM, and Redis together are ~32%. This workload is bound by per-request Python overhead, not by disk.
