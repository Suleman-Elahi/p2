# p2 Storage Engine — Architecture & Hot-Path Review

Scope: the append-only "Volume Pool" storage engine that replaced the original
one-file-per-object layout. Based on a read of `p2/s3/volume_pool.py`,
`volume_writer.py`, `volume_reader.py`, `engine.py`, `compaction.py`,
`p2/core/settings.py`, `p2/s3/views/objects.py`, `multipart.py`, `buckets.py`,
`asgi_handler.py`, and `p2/core/volume_stats.py`.

## 1. Overview

Objects used to be stored as one physical file per blob under
`internal-storage/volumes/<vol>/<shard>/<uuid>`. That locator survives only as
a synthetic `internal_path` field in metadata (kept so existing rows still
parse); no proxy or `sendfile()` handoff consumes it any more. New writes go
through the **Volume Pool**:

```
PUT /bucket/key
   │
   ▼
ObjectView.put()  (or ASGI fast-path)
   │  1. buffer + hash body (MD5 + SHA256)
   │  2. VolumePool.allocate_block(len) -> (VolumeHandle, offset)
   │  3. write_block(handle, offset, data, engine, key, metadata_json)
   │        │
   │        ├─ enqueue on per-worker asyncio.Queue  ─────┐
   │        │                                            │
   │        ▼                                            ▼
   │   await future                             _commit_worker (1 per event loop)
   │                                              drains queue → batch
   │                                              pwrite() each item
   │                                              fdatasync() once per volume touched
   │                                              ONE LMDB write txn for all metadata
   │                                              resolve futures
   ▼
 200 OK (only after fdatasync + LMDB commit resolve the future)
```

Physical bytes live in fixed-size preallocated flat files:
`STORAGE_ROOT/volumes/vol_<uuid>.bin`, default **10 GiB**, `VOLUME_ACTIVE_POOL_SIZE`
(default 4) of them open for writing concurrently per process. Metadata
(`{size, mime, blocks: [{vol_uuid, offset, length}], etag, sha256, mtime, ...}`)
is stored as JSON in a per-**Volume** (tenant/bucket) **LMDB** database
(`metadata.lmdb`), not in Django ORM/SQLite — the ORM `Volume` model only
tracks aggregate `object_count` / `space_used_bytes`.

## 2. Write path (append-only)

**Allocation** (`volume_pool.py`): each `VolumeHandle` has an in-process
`threading.Lock`-guarded offset counter. `allocate_block(length)` claims a
byte range with a simple `if offset+length > size_limit` check, no data is
written yet. When all pool slots are full, the pool seals the full ones
(`chmod 0o444`, removes from active pool) and opens fresh preallocated `.bin`
files. Multiple concurrently-active volumes exist specifically so PUTs don't
serialize on one file's offset lock.

**Group commit** (`volume_writer.py`): this is the core durability/throughput
mechanism.
- Each PUT pushes `(handle, offset, data, engine, key, metadata_json, future)`
  onto a bounded `asyncio.Queue` (default max 8192) and awaits the future —
  non-blocking suspension, not a thread block.
- A single background `_commit_worker` task per event loop drains up to
  `S3_METADATA_WRITE_BATCH_SIZE` (128) queued items into one batch, then:
  1. Groups by target volume, issues all `os.pwrite()` calls.
  2. Issues **one `fdatasync()` per distinct volume touched** in the batch
     (not per-object) — if a batch spans >1 volume the fsyncs run concurrently
     via a `ThreadPoolExecutor`.
  3. Commits **all** metadata JSON blobs in **one LMDB write transaction**.
  4. Resolves every future (success/exception).
- `S3_METADATA_WRITE_BATCH_WINDOW_MS` defaults to **0** deliberately — the
  code comment cites a measured 1.3ms/op (window=0) vs 7.3ms/op (window=5ms)
  at low per-worker concurrency, i.e. waiting for stragglers is a net loss
  unless arrival rate genuinely exceeds flush rate.
- Fallback: if the queue is full, `_direct_write` does a synchronous
  pwrite + fdatasync + LMDB txn per request (no batching, higher latency
  under overload but still correct).

This is where "no fsync-per-file" pays off: N concurrent single-object PUTs
that used to mean N `fsync(2)` calls now cost ~1 fsync per volume per batch
window. Durability is preserved — the HTTP response future is only resolved
*after* the fdatasync and LMDB commit complete.

**Durability is controlled by a single setting**, `S3_DURABLE_WRITES`
(`settings.py`, default **True**), which drives `S3_VOLUME_FDATASYNC` +
`S3_METADATA_LMDB_SYNC` + `S3_METADATA_LMDB_METASYNC` together. This used to
be five independently toggleable knobs that could be combined into
confusing or unsafe states (e.g. queue disabled + sync enabled, which is
what caused the 39 obj/s PUT regression described in §9). They were
collapsed into one lever because group-commit batching (always on) makes
full durability cheap — there's no real throughput reason to run with
`S3_DURABLE_WRITES=False` in production; it exists only for local
scratch/benchmark environments.

## 3. Read path

`volume_reader.py` reads directly via `pread(2)` at the `(vol_uuid, offset,
length)` coordinates stored in LMDB — no per-object file open on the common
path, no data copy beyond the syscall buffer.

Three sizing tiers:
- **≤ 64 KiB**: `pread` inline in the event loop (comment: page-cache hit is
  ~1–5us, cheaper than the ~30–100us `asyncio.to_thread` dispatch).
- **64 KiB – 4 MiB**: `pread` via thread pool + `posix_fadvise(RANDOM)`.
- **> 4 MiB**: async-generator streaming in 4 MiB chunks with
  `posix_fadvise(SEQUENTIAL)`.

An `_FDCache` (max 64 fds, 30s idle eviction, thread-safe) avoids
open+close syscalls per read — important since volume files are shared
across many objects, unlike the old one-fd-per-object model.

Range GETs go through `slice_blocks()` → per-block clamped `(offset, length)`
reads, same tiering.

An optional Rust io_uring streamer (`p2_s3_crypto`) can serve large/range
reads; small reads intentionally bypass it (comment notes the uring
hand-off/dup(2) round-trip costs more than a page-cache-hit pread and
serializes what should be parallel).

There's also a second, independent hot path: `asgi_handler.py` intercepts S3
GET/PUT/DELETE **before Django middleware/routing** for the common case
(bucket.s3domain or /bucket/key with AWS SigV4), doing its own header
parsing, auth, and inline reads streamed straight back by Granian.

## 4. Metadata engine (LMDB)

`engine.py` — one `lmdb.Environment` per Volume, opened with:
`subdir=False`, `readahead=False` (accesses are random, not sequential),
`meminit=False`, `max_readers=1024`, `map_size=256GiB` (virtual only, no
physical allocation until keys are written). Reads are lock-free mmap
snapshots; writes use LMDB's single-writer transaction model, consistent
with the "one LMDB txn per batch" group-commit design.

`list()` / `list_dir()` use B-tree cursor `set_range` + prefix scanning for
S3 ListObjects, with an application-level prefix cache
(`p2.s3.cache`) invalidated on put/delete.

DELETE is logical only — it removes the LMDB key; physical bytes are left in
the `.bin` file until compaction runs. This is correct for an append-only log
but means disk usage is not reclaimed synchronously with delete.

## 5. Compaction / GC (`compaction.py`)

Scheduled as a background ARQ cron job, entirely off the request path:

1. **Full scan** of every LMDB entry across **every** volume to compute, per
   sealed volume, `live_bytes / actual_file_size`.
2. If ratio < `VOLUME_COMPACT_THRESHOLD` (default 30%), migrate every
   surviving block: `pread` old bytes → `pool.allocate_block()` into a new
   *active* volume → `pwrite` + `fdatasync` → rewrite the owning object's
   LMDB metadata with the new block coordinates → finally `os.remove()` the
   old sealed volume file.
3. Runs sequentially, one object/key at a time, with `asyncio.to_thread` for
   each blocking syscall.

## 6. Configuration surface (`p2/core/settings.py`)

| Setting | Default | Effect |
|---|---|---|
| `VOLUME_SIZE_BYTES` | 10 GiB | size of one `.bin` before sealing |
| `VOLUME_ACTIVE_POOL_SIZE` | 4 | concurrently writable volumes/process |
| `VOLUME_COMPACT_THRESHOLD` | 0.30 | live-byte ratio that triggers compaction |
| `S3_DURABLE_WRITES` | True | single lever: fsync object bytes + LMDB sync/metasync |
| `S3_METADATA_WRITE_QUEUE_MAX_SIZE` | 8192 | backpressure threshold (queue is always on) |
| `S3_METADATA_WRITE_BATCH_SIZE` | 128 | max items per fsync/LMDB-txn batch |

Straggler-wait (formerly `S3_METADATA_WRITE_BATCH_WINDOW_MS`) is hardcoded
to 0 and no longer configurable — a non-zero wait was measured to add
latency (7.3ms/op at 5ms window vs 1.3ms/op at 0ms) without a throughput
benefit at realistic per-worker concurrency.

---

## 7. Flagged performance issues / hot-path hurdles

**Full in-memory body buffering on PUT** (`views/objects.py::put`,
`asgi_handler.py`) — the entire request body is read into a `list[bytes]`,
joined into one `bytes`, then hashed with MD5 *and* SHA256 sequentially,
*before* any byte reaches disk. For large objects (multi-GB) this means:
2x request-body memory
allocated per concurrent upload (raw chunks list + joined buffer), plus two
full-buffer hash passes, all before `allocate_block`/`write_block` even
starts. This is the single largest latency and memory-pressure risk in the
write path, independent of the volume-pool design itself. Streaming
hash-as-you-read plus writing to a pre-allocated offset incrementally would
cut peak memory and TTFB for large uploads substantially. Multipart parts go
through the same full-buffer pattern (`multipart.py`), so this affects large
single-object PUTs and each individual multipart part.

**LMDB metadata durability is off by default, and recovery is scan-based.**
This is a deliberate, documented tradeoff, but worth flagging plainly: if the
process crashes between the `fdatasync` on the volume file and the LMDB
commit, or if LMDB's own on-disk B-tree page write is lost (metasync=False
means dirty pages can be lost on power failure, not just process crash),
recovery requires `scan_volume_stats`-style full-volume rescans to rebuild
index entries — but there is no code path found that actually reconstructs
individual object→block LMDB entries from raw volume bytes (only counters
are rebuilt via `scan_volume_stats`; the blocks themselves are opaque binary
regions with no on-disk index or magic/length framing found in
`volume_pool.py`). If the LMDB entry for an object is lost, its bytes are
**unreachable and unrecoverable** — there's no self-describing block header
to reconstruct from. This is the most consequential correctness/durability
gap: fsync-skipping on the index only rebuildable-in-theory if the block
layout were self-describing, and today it is not.

**Compaction is O(all objects across all volumes) per run**, single-threaded,
one object at a time (`compaction.py`). At petabyte/many-millions-of-objects
scale this will become a long-running background job that re-scans
everything on every cron tick even when only a few sealed volumes are below
threshold. There's no incremental/delta tracking of per-volume live-byte
count as deletes happen (it's recomputed by a full scan every time) —
maintaining a live counter per volume, decremented on logical delete and
incremented on migrate, would turn this into an O(candidates) job instead of
O(all objects).

**Compaction contends with the same `VolumePool.allocate_block()` /
group-commit path used by live traffic.** Migrated blocks are written via
the *same* pool and (in `_migrate_block`) a direct `pwrite` + `fdatasync` per
block outside the batching group-committer — i.e. compaction traffic
generates one fsync per migrated block, not batched, and competes for the
same active-volume write locks that live PUTs use. Under heavy compaction,
foreground PUT latency could degrade due to fsync contention on shared
volume fds. There's no throttling/rate-limiting of the migration loop.

**Metadata list/scan (`engine.list`, `list_dir`) takes an exclusive-ish read
snapshot but is O(matching keys) per call** and is used both by user-facing
ListObjects and by the compaction scanner and `scan_volume_stats`. Given
`readahead=False` is set deliberately for random object access, a full
`list('')` scan (as compaction and stats-recalc do) will be page-fault-heavy
across the whole B-tree; no evidence of a maintained sorted index or
secondary "live blocks" table to avoid full scans.

**Two independent request-handling paths** (`ObjectView` in
`views/objects.py` and the ASGI fast-path in `asgi_handler.py`) duplicate
metadata parsing, block-coordinate extraction, and content-negotiation
logic. Not a runtime performance issue per se, but a maintenance/consistency
risk: a fix to one (e.g. legacy-object fallback, versioning, tagging) must be
mirrored in the other or the fast path silently falls back to Django for
cases it doesn't handle (e.g. `blocks_raw` empty → legacy fallback), which is
correct but means the "fast path" quietly degrades for any bucket still
holding legacy one-file-per-object data.

**Dead/broken code path**: `versioning.py::delete_specific_version_sync` and
`views/buckets.py::_multi_delete` both `from p2.core.storage_path import
internal_to_fs` and call it against `internal_path` metadata. The function
exists in `p2/core/storage_path.py` and works — so not currently broken —
but it's exercising the *legacy* one-file-per-object filesystem deletion
logic against objects that, under the volume-pool model, have no real file
at `internal_path` (it's now a synthetic placeholder string retained for
metadata compatibility, not a materialized path with real bytes).
`os.remove()` on that path will raise `OSError`/`FileNotFoundError`
every time for volume-pool objects, which is silently swallowed
(`except OSError: pass`) — harmless today, but it's dead weight running on
every delete of a modern object and would mask a real problem if the
`internal_path` scheme is ever repurposed.

**Volume stats counters route through Redis + a 2-second async flush loop**
(`volume_stats.py::adjust_volume_stats`) on every PUT/DELETE — an extra
network round trip (pipelined, so cheap, but still inter-process) per
object mutation, independent of the volume-pool write itself. Not part of
the append-only design proper, but it's on the same hot path and adds a
Redis dependency + a background asyncio task per process that must survive
graceful shutdown (the `_flush_loop` exits after `_DIRTY_VOLUMES` drains,
meaning a quiet period with no traffic stops the loop — it's correctly
restarted on the next `adjust_volume_stats` call, but there's a window where
counters sit dirty in Redis un-flushed for up to 2s if the process is killed
non-gracefully).

**No backpressure signal to the client when the batching queue is full** —
`write_block` catches `asyncio.QueueFull` and falls back to `_direct_write`,
which is correct for correctness but means load spikes silently degrade
every request to the slow, non-batched, individual-fsync path rather than
applying backpressure (e.g. 503/Retry-After) upstream. Under sustained
overload this could produce a thundering herd of individual fsyncs, the
exact problem group-commit was designed to avoid.

## 9. Fixed: PUT throughput regression + config sprawl

Benchmarking after the redesign showed PUT at ~39 obj/s (524ms avg latency,
1.26s worst) against a healthy ~4700 obj/s GET. Two independent problems
were found and fixed:

1. **`.env` had the group-commit queue disabled** (`WRITE_QUEUE__ENABLED=false`)
   while durability was fully on (`LMDB SYNC=true, METASYNC=true`). Without
   batching, LMDB's single-writer lock serializes every concurrent PUT behind
   a per-object fsync + LMDB commit — confirmed by direct measurement on the
   deployment disk (~10-15ms per unbatched commit vs. ~0.15ms/item-equivalent
   batched at 64 items/txn, fully durable in both cases).
2. **Both ASGI and RSGI fast-path S3 handlers** (`asgi_handler.py`,
   `rsgi_handler.py` — the code path actually serving requests when Granian
   runs in `asginl`/`rsgi` mode, ahead of the Django views) were calling
   `engine.put()` directly for the metadata commit instead of routing through
   `write_block()`/the group committer. This meant the batching engine was
   never exercised on the hot path regardless of the `.env` queue setting.
   Fixed to route through `write_block()` so metadata commits are coalesced
   with other concurrent PUTs into a single fdatasync + LMDB transaction,
   matching the Django view's write path.

The five separate durability/batching knobs (`S3_METADATA_LMDB_SYNC`,
`S3_METADATA_LMDB_METASYNC`, `S3_VOLUME_FDATASYNC`, `S3_METADATA_WRITE_QUEUE_ENABLED`,
`S3_METADATA_WRITE_BATCH_WINDOW_MS`) were also collapsed into a single
`S3_DURABLE_WRITES` lever plus two batch-sizing knobs, since batching is not
optional/experimental — it's what makes full durability cheap — and the old
"choose fast-unsafe or slow-safe" framing was a false dichotomy.

Measured after the fix (local dev, warp, 20 concurrency, 4 KiB objects, fully
durable): PUT went from 39 obj/s / 524ms avg to 268 obj/s / 70ms avg — a ~7x
improvement with no durability given up. GET was unaffected (no GET code was
touched).

## 10. Fixed: wire-protocol corruption on ASGI fast-path PUT (aws-chunked)

A follow-up benchmark run surfaced HTTP keep-alive desync on GET
(`"Unsolicited response received on idle HTTP channel starting with \x00\x00..."`).
Root cause was in `asgi_handler.py`'s PUT handler, specifically the
`aws-chunked` upload path (the encoding most S3 SDKs, including warp, use by
default):

- The volume block was allocated using the raw wire `Content-Length`, which
  includes AWS chunk-framing overhead (hex-size headers, optional
  `chunk-signature=...`, `\r\n` delimiters) — not the actual decoded payload
  size. `decode_aws_chunked()` was also called independently on each raw
  ASGI network fragment, which is unsound because chunk boundaries can
  straddle two separate `receive()` messages.
- Since volume `.bin` files are sparse (`os.truncate`-backed, zero-filled),
  the gap between the real object end and the over-recorded block length was
  all zero bytes. On GET, `read_object()` read the full (wrong, larger)
  block length and streamed the zero padding as part of the response body,
  while `Content-Length` correctly reported the smaller real size. The
  client consumed exactly `Content-Length` bytes, leaving the leftover
  `\x00` bytes sitting in the TCP stream to be misread as the start of the
  next keep-alive response — silent data corruption, not benchmark noise.

Fixed by buffering the full raw body for `aws-chunked` uploads, decoding
once, and allocating the block from the *decoded* size — matching what
`views/objects.py` (Django view) and `rsgi_handler.py` already did
correctly. Also added a strict length check on the non-chunked path: if the
bytes actually received don't match the declared `Content-Length`, the
upload is now rejected (`400 IncompleteBody`) instead of silently recording
a block length that doesn't match the real payload.

Verified with warp PUT+GET at both 4 KiB and 1 MiB object sizes after the
fix: no corruption warnings, PUT ~400 obj/s / GET ~2270 obj/s at 4 KiB,
concurrency 20. Full `p2.s3` test suite (73 tests) still passes.

## 8. Summary

The append-only volume-pool design achieves its stated goal: PUT throughput
under concurrency is bounded by ~1 fsync per batch per volume instead of 1
per object, LMDB gives lock-free mmap reads for metadata, and `pread`-based
reads avoid a filesystem lookup/open per object. The largest risks are not
in the append/fsync mechanics themselves but around it: full in-memory
body buffering on PUT, an LMDB metadata index that is unrecoverable (not
just "needs a rebuild pass") if lost since blocks aren't self-describing,
full-scan compaction that will not scale with object count, and compaction
write traffic sharing an unbatched fsync path with live PUTs.
