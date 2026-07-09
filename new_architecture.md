This is the revised, production-grade architecture for **P2**.

By shifting from a naive "one-file-per-object" layout to a **Fixed-Size Volume Pool (with io_uring, Group Committing, and zero-copy Metadata-only Multiparts)**, P2 bypasses traditional POSIX filesystem directory bottlenecks, inode limits, and double-syncing performance hits.

---

In your Cargo.toml:

[dependencies]
tokio-uring = { version = "0.5.0" }

### Revised P2 Architecture Diagram

```
                          ┌─────────────────────────────────────────────────┐
                          │                  CLIENTS                        │
                          │   S3 SDKs (aws-cli, boto3)   │   Web Browser    │
                          └───────────┬─────────────────┬─┴──────┬──────────┘
                                      │                 │        │
                          ┌───────────▼──┐    ┌────────▼──┐ ┌──▼───────────┐
                          │  S3 API (:80) │    │ Admin API  │ │   Web UI    │
                          │  AWS v4 Auth  │    │ (:8787)    │ │  Vue 3 SPA  │
                          │  XML Responses│    │ REST/JSON  │ │ frappe-ui   │
                          └──────┬────────┘    └─────┬──────┘ └──────┬───────┘
                                 │                   │               │
                          ┌──────▼───────────────────▼───────────────▼───────┐
                          │                   NGINX                          │
                          │  • Routes S3 paths vs admin/UI paths             │
                          │  • Handles static frontend assets                │
                          │  • proxy_request_buffering off (streaming PUTs)  │
                          └──────────────────────┬───────────────────────────┘
                                                 │
                          ┌──────────────────────▼──────────────────────────┐
                          │              GRANIAN SERVER (:8787)              │
                          │              8 workers • uvloop                  │
                          │                                                 │
                          │  ┌──────────────────┐  ┌──────────────────────┐ │
                          │  │ RSGI/ASGI        │  │   DJANGO CONTROL     │ │
                          │  │ FAST PATH        │  │   PLANE (Ninja)      │ │
                          │  │                  │  │                      │ │
                          │  │ Simple PUT/GET:  │  │ Complex / Auth Ops:  │ │
                          │  │ - Auth validation │  │ - IAM & S3 Policies  │ │
                          │  │ - Volume routing  │  │ - Bucket operations  │ │
                          │  │ - Metadata lookup │  │ - Multipart init     │ │
                          │  │ - Streaming reads │  │ - Copy / Tagging     │ │
                          │  └────────┬─────────┘  └──────────┬───────────┘ │
                          └───────────┼────────────────────────┼────────────┘
                                      │                        │
      ┌───────────────────────────────┼────────────────────────┼─────────────────┐
      │                               ▼                        ▼                 │
      │         ┌─────────────────────────────────────────────────────┐         │
      │         │               RUST STORAGE ENGINE (PyO3)            │         │
      │         │                                                     │         │
      │         │  p2_storage.so                                      │         │
      │         │  ┌────────────────────────────────────────────────┐ │         │
      │         │  │ • io_uring Disk Writer (Thread-free async I/O) │ │         │
      │         │  │ • Group Committer (Coordinated batch flushes)  │ │         │
      │         │  │ • Inline Cryto & Hashes (AES-GCM-256 / SHA256) │ │         │
      │         │  │ • Zero-copy Streamer (splice/sendfile wrappers)│ │         │
      │         │  └────────────────────────────────────────────────┘ │         │
      │         └──────────────────────────────────┬──────────────────┘         │
      │                                            │                             │
      │    ┌───────────────────────────────────────┴────────────────────────┐    │
      │    │                    PHYSICAL DATA STORES                        │    │
      │    │                                                                │    │
      │    │  VOLUME FILES (Flat Chunks)      LMDB (Metadata Index)         │    │
      │    │  ┌─────────────────────────┐     ┌─────────────────────────┐   │    │
      │    │  │ • storage/volumes/      │     │ • Key: S3 Object Path   │   │    │
      │    │  │   vol_{uuid}.bin        │     │ • Val: Size, MIME,      │   │    │
      │    │  │ • Preallocated (10GB)   │     │   Blocks: [             │   │    │
      │    │  │ • Sealed (Read-Only) vs │     │     {vol, offset, len}, │   │    │
      │    │  │   Active (Append-Only)  │     │     ...                   │   │    │
      │    │  │ • Encrypted payload     │     │   ], SSE metadata, etc. │   │    │
      │    │  └─────────────────────────┘     └─────────────────────────┘   │    │
      │    └────────────────────────────────────────────────────────────────┘    │
      │                                                                          │
      └──────────────────────────────────────────────────────────────────────────┘

      ┌──────────────────────────────────────────────────────────────────────┐
      │                     SUPPORTING SERVICES                              │
      │                                                                      │
      │  DRAGONFLY / REDIS              PostgreSQL / libSQL (Turso)          │
      │  ┌──────────────────────────┐    ┌──────────────────────────────┐    │
      │  │ DB 0: Django cache       │    │ Django ORM control data:     │    │
      │  │  • Sessions              │    │ • Volume & Bucket schemas    │    │
      │  │  • Volume routing state  │    │ • User accounts & IAM Keys   │    │
      │  │                          │    │ • API Key validation         │    │
      │  │ DB 1: ARQ message queue  │    │                              │    │
      │  │  • Compaction tasks      │    └──────────────────────────────┘    │
      │  │                          │                                        │
      │  │ DB 2-4: Redis Streams    │    ARQ COMPACTION WORKER               │
      │  │  • Event consumer groups │    ┌──────────────────────────────┐    │
      │  │    - replication         │    │ • run_compaction:            │    │
      │  │    - background GC       │    │   Scans sealed volumes,      │    │
      │  │    - metadata backup     │    │   migrates surviving bytes,  │    │
      │  │  • Dead letter stream    │    │   updates LMDB, deletes old  │    │
      │  └──────────────────────────┘    │   volume files to reclaim space│    │
      │                                  └──────────────────────────────┘    │
      └──────────────────────────────────────────────────────────────────────┘
```

---

### Detailed Component Functioning

#### 1. Ingress & Routing (Nginx & Granian)
* **Nginx:** Handles TLS termination and parses the inbound S3 request. It acts as a reverse proxy, bypassing request buffering (`proxy_request_buffering off`) to stream large object payloads directly to Granian.
* **Granian (Fast Path):** An ultra-fast Python web server built on Rust (`uvloop` under the hood). Simple `GET` and `PUT` requests bypass the heavy Django middleware entirely.
* **Django Control Plane (Ninja):** Handles logical administration, bucket creation, IAM validation, bucket policy evaluation, and generation of Presigned URLs. It writes to PostgreSQL/libSQL.

#### 2. Rust Storage Engine (`p2_storage.so`)
This is the core of the optimized system. Built using PyO3, it contains the critical data-path mechanisms:
* **`io_uring` Core:** Bypasses Python’s synchronous file API and thread pools. Disk operations (reads/writes) are submitted directly to the Linux kernel ring buffer.
* **Active Volume Allocator:** Manages a pool of active write-handles (e.g., up to 8 preallocated 10GB `.bin` files on disk). This distributes write load and eliminates file lock contention.
* **Group Committer:** Queues incoming disk writes and metadata updates. Instead of running a costly `fdatasync` per request, it flushes writes to the active volume and commits the metadata to LMDB in batches (e.g., every 4ms or 50 writes).
* **Inline Cryptography & Hashing:** Integrates AES-GCM-256 (for SSE-S3/SSE-C) and calculates SHA256/MD5 checksums *inline* as the data stream is fed to `io_uring`, eliminating the need for a separate pass over the data.

#### 3. Storage Layer (Physical Volumes & LMDB)
* **Physical Volumes (`.bin`):** Preallocated files of a fixed size (e.g., 10GB).
  * *Active volumes* are appended to sequentially.
  * *Sealed volumes* are marked read-only and immutable, which is ideal for performance and caching.
* **LMDB (The Metadata Engine):** Key-value B+Tree mapping S3 logical paths to a structural list of blocks.
  * **Key:** `bucket-name/object-key`
  * **Value (JSON/MessagePack):**
    ```json
    {
      "size": 52428800,
      "mime": "application/octet-stream",
      "blocks": [
        {"vol_uuid": "vol_a1b2...", "offset": 1048576, "length": 26214400},
        {"vol_uuid": "vol_c3d4...", "offset": 0, "length": 26214400}
      ],
      "sse_algorithm": "AES256",
      "etag": "1b2cf..."
    }
    ```

#### 4. Background Compaction & Lifecycle (ARQ Worker)
Because physical volume files are append-only, deleting an object simply marks its space as "dead" by deleting the LMDB index.
* **ARQ Compaction Worker:** Monitors fragmentation metrics. If a sealed volume drops below a specific threshold of active data (e.g., < 30% active), the worker sweeps the volume, appends the remaining valid blocks to a new active volume, updates their entries in LMDB, and deletes the old volume file.

---

### Optimized Lifecycle Data Flows

#### PUT Object (Small & Medium Objects)

```
[Client]   [Granian Fast Path]       [Rust Engine (io_uring)]       [Active Vol]   [LMDB]
   │                │                           │                        │            │
   │───PUT Object──>│                           │                        │            │
   │   (Stream)     │───Stream bytes (PyO3)────>│                        │            │
   │                │                           │───Encrypt/Hash inline  │            │
   │                │                           │───Append to queue      │            │
   │                │                           │                        │            │
   │                │                           │─[Group Commit Trigger] │            │
   │                │                           │───io_uring write─────>│            │
   │                │                           │   & fdatasync()        │            │
   │                │                           │───Commit batch to──────────────────>│
   │                │                           │   LMDB (sync=true)     │            │
   │                │<───Success (offset/len)───│                        │            │
   │<───200 OK──────│                           │                        │            │
```

1. **Ingress:** Client streams the object payload. Nginx passes it to Granian, which streams the buffer to the Rust storage engine.
2. **Processing:** The Rust engine encrypts the incoming stream inline (if SSE is active) and computes the MD5/SHA256 checksums without copying memory.
3. **Queueing:** The engine claims the next available offset in an **Active Volume** and queues the write.
4. **Group Commit:** The Group Committer waits for a maximum of $N$ milliseconds or $M$ requests, writes the batch to the active `.bin` file via `io_uring`, triggers `fdatasync()` on the volume, and writes the batch of metadata to LMDB in a single transaction.
5. **Response:** A `200 OK` along with the ETag is returned to the client.

---

#### PUT Object (Multipart Uploads - Zero-Copy)

Unlike traditional S3 engines that stitch physical files on disk (causing massive read/write amplification), P2 uses **metadata assembly**:

1. **Initiate:** Django Control Plane registers a new multipart upload ID in Redis/Postgres.
2. **UploadPart:** The client uploads Part 1 (e.g., 50MB). The system treats it exactly like a standard PUT:
   * The bytes are appended to the current **Active Volume**.
   * Instead of committing to the active S3 key, the resulting block coordinate `{"vol_uuid": "...", "offset": ..., "length": ...}` is recorded in a temporary LMDB namespace keyed by `upload_id/part_number`.
3. **CompleteMultipartUpload:** The client calls Complete.
   * The Django Control Plane reads the block coordinates for all part numbers.
   * It compiles them into a single ordered array of blocks.
   * It writes a single metadata entry to LMDB under the target S3 path containing this block array.
   * **Zero physical bytes are copied, moved, or stitched on disk.** The operation is instantaneous and uses zero I/O.

---

#### GET Object (Streaming & Range Requests)

```
[Client]       [Nginx]       [Granian Fast Path]       [LMDB]       [Rust Engine]   [Vol File]
   │              │                   │                  │                │             │
   │───GET /obj──>│                   │                  │                │             │
   │              │───Forward GET────>│                  │                │             │
   │              │                   │───Read metadata─>│                │             │
   │              │                   │<──[vol, off, len]│                │             │
   │              │                   │                                   │             │
   │              │                   │───Call zero-copy stream──────────>│             │
   │              │                   │                                   │──pread()───>│
   │<──Stream bytes───────────────────────────────────────────────────────│<──[Bytes]───│
```

1. **Request:** The client requests an object (or a specific byte range of an object).
2. **Authorization & Metadata Lookup:** Granian validates the S3 signature, checks the cache (or queries LMDB directly) to fetch the block array, and determines the offset and length.
3. **Decryption Check:** If the metadata specifies that the object is encrypted, the decryption key is derived.
4. **Streaming Execution:**
   * Granian invokes the Rust engine's streaming interface.
   * Under Linux, the Rust engine uses `splice()` or optimized `pread()` loops via `io_uring` to stream the bytes directly from the preallocated `.bin` file at the specified offset and length, bypassing user-space buffer copies where possible.
   * Decryption is performed chunk-by-chunk on the fly before writing to the network socket.

---

#### DELETE Object & Compaction (Garbage Collection)

```
[Client]       [Granian Fast Path]       [LMDB]       [ARQ Worker]       [Sealed Vol]   [Active Vol]
   │                    │                  │               │                  │              │
   │───DELETE /obj─────>│                  │               │                  │              │
   │                    │───Delete Key────>│               │                  │              │
   │<──204 No Content───│                  │               │                  │              │
                        │                  │               │                  │              │
                        │                  │   [Compaction Timer]             │              │
                        │                  │───Scan LMDB for usage───────────>│              │
                        │                  │   (vol_012.bin is 75% dead)      │              │
                        │                  │               │                  │              │
                        │                  │               │───Read active───>│              │
                        │                  │               │   bytes (25%)    │              │
                        │                  │               │────────────────────────────────>│
                        │                  │               │   Append survivors to active    │
                        │                  │<──Update LMDB─│                  │              │
                        │                  │   metadata    │                  │              │
                        │                  │               │───Delete physical file─────────X
```

1. **Deletion:** The client sends a `DELETE` request.
2. **Logical Deletion:** The Granian Fast Path deletes the logical key from LMDB. The physical data inside the volume file remains untouched. The client immediately receives a `204 No Content` response.
3. **Compaction Assessment:** A periodic ARQ background task scans LMDB records to calculate the active byte ratio of sealed volumes.
4. **Compaction Execution:** If `vol_012.bin` (a 10GB file) has only 2GB of active objects associated with it in LMDB:
   * The ARQ worker reads those 2GB of active objects sequentially.
   * It appends them to a currently open **Active Volume**.
   * It updates the LMDB blocks index for those objects to point to the new volume and offsets.
   * It deletes the physical `vol_012.bin` file from the disk, reclaiming 10GB of storage.
