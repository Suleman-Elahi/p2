# Milliscale-Inspired Optimizations for p2's Group Commit Path

Source paper: [Milliscale: Fast Commit on Low-Latency Object Storage](https://arxiv.org/abs/2603.02108) (Zhou, Huang, Wang — SFU/UCalgary, arXiv:2603.02108v1, cs.DB, Mar 2026).

## Why this paper, and what actually transfers

Milliscale is an OLTP engine that commits its write-ahead log directly to Amazon
S3 Express One Zone. p2 is not an OLTP engine and does not write a WAL to S3, so
most of the paper (commit sequence numbers, MVCC-aware recovery, checkpointing,
log segmentation into S3 objects) does not apply here.

What *does* transfer is the shape of the problem Milliscale solves in
Section 4: a **shared batch/group-commit path** where multiple independent
callers wait on a single flush-and-durability operation, and where naive
batching creates commit-latency inflation through (a) too many durability
syscalls and (b) unrelated callers being coupled together ("false positive
dependencies"). `p2/s3/volume_writer.py`'s group committer is exactly this
shape: concurrent PUTs share a batching window, a single io_uring write call,
and per-engine LMDB commits.

Two of Milliscale's techniques map onto concrete, already-present code paths:

- **Record-level dependency tracking** (§4.3) → p2's batch committer currently
  resolves every job's completion future only after the *whole* batch
  function returns, regardless of which LMDB engine/volume the job belongs
  to. This is the same "false positive dependency" pattern Milliscale
  identifies: work that has actually finished still waits on unrelated work.
- **Frugal, right-sized durability operations** (§3, "Frugal on Object
  Operations") → Milliscale batches log flushes and calls fdatasync once per
  unique fd per batch instead of once per write. p2's Rust `GroupCommitter`
  (`group_commit.rs`) already does this, but it is **not currently wired up**
  — the active Python path (`_write_and_commit_batch`) never calls fdatasync
  at all, despite `S3_VOLUME_FDATASYNC` in `core/settings.py` documenting it
  as a durability guarantee.
- **Buffer/batch sizing against measured backend latency** (§4.2) →
  Milliscale tunes log buffer size against S3 Express One Zone's actual
  request-size latency curve rather than guessing. p2's
  `S3_METADATA_WRITE_BATCH_SIZE` / `_BATCH_WINDOW_SECONDS` /
  `S3_METADATA_WRITE_BATCH_WINDOW_MS` look like reasonable defaults but
  don't appear to be derived from measuring io_uring + LMDB commit latency
  under load.

## Current state (verified by reading the code)

- `p2/s3/volume_writer.py`
  - `_ensure_batch_worker` / `_batch_worker`: one `asyncio.Queue` per Granian
    worker event loop, batches up to `S3_METADATA_WRITE_BATCH_SIZE` (default
    128) jobs, with a `_BATCH_WINDOW_SECONDS = 0.001` coalescing wait only
    when the batch is small (`< 4` jobs).
  - `_write_and_commit_batch`: calls `p2_s3_crypto.write_blocks_uring(...)`
    once for **all** jobs in the batch (mixed engines/volumes), then loops
    per-engine to do one `env.begin(write=True)` LMDB transaction per engine.
  - **No fdatasync call anywhere in this function.** `S3_VOLUME_FDATASYNC`
    (`core/settings.py:101`) is documented as controlling "whether the group
    committer issues fdatasync on the volume file after each batch" but is
    not read by any Python code (confirmed via repo-wide search).
  - `_batch_worker` resolves **every** job's `completion` future together,
    after `_write_and_commit_batch` returns for the entire batch — not
    per-engine.
- `p2/s3/rust_ext/src/group_commit.rs`: a `GroupCommitter` PyO3 class exists
  that batches writes, joins all write threads, then calls
  `libc::fdatasync` once per **unique fd** in the batch. This class is not
  imported or used anywhere in `volume_writer.py` today (per `plan.txt`,
  Phase 2 Rust work is still TODO / not wired in).
- `p2/s3/rust_ext/src/io_uring_writer.rs`: exposes a separate
  `fdatasync_uring(fd)` pyfunction, also unused by the current write path.

## Plan

### 1. Fix the false-positive dependency in `_write_and_commit_batch` (correctness + latency)
Resolve each job's completion as soon as *its own* engine's write + LMDB
commit is done, not when the whole cross-engine batch finishes.

- Group jobs by engine **before** the io_uring call, same as today.
- After each engine's LMDB transaction commits, immediately resolve the
  completion futures for that engine's jobs (call `job.completion.set_result`
  inside the per-engine loop, guarded appropriately since we're off the event
  loop thread — use `loop.call_soon_threadsafe`).
- Keep the single `write_blocks_uring` call across the batch (io_uring
  batching data writes together is still a genuine throughput win — this
  isn't the part causing false dependencies, the per-batch *future resolution*
  is).
- Net effect: a PUT to a quiet volume/engine no longer waits on a busy,
  unrelated volume/engine's LMDB commit just because they landed in the same
  1ms window.

### 2. Decide fdatasync policy explicitly and make `S3_VOLUME_FDATASYNC` real
Right now the setting exists, is documented as a durability guarantee, and is
silently not enforced. Two options, pick one deliberately:

- **Option A (match the docs):** call `fdatasync` on each unique volume fd
  touched by the batch, once per batch, gated by `S3_VOLUME_FDATASYNC`. This
  is the direct Milliscale-style "one durability syscall per batch, not per
  write" application. Reuse the existing `fdatasync_uring` Rust pyfunction
  (already exposed, unused) instead of reimplementing in Python — call it via
  `asyncio.to_thread` or fold it into the same `to_thread` call as the batch
  write/commit.
- **Option B (change the docs):** if the intent is genuinely "durability is
  provided by other means (e.g. LMDB is rebuildable, OS page cache is
  acceptable risk)," update the setting's docstring and default to reflect
  that fdatasync is not called, rather than leaving a guarantee documented
  that the code doesn't provide.

This should be resolved regardless of anything else in this plan — it's a
correctness/documentation mismatch independent of Milliscale.

### 3. Right-size the batch window against measured latency (Milliscale §4.2 style)
Milliscale picked its buffer size and thread-sharing ratio by measuring the
S3 Express One Zone latency curve and finding the plateau. Do the analogous
measurement for p2's batch committer:

- Benchmark commit latency (time from `write_block` enqueue to completion)
  and throughput across a matrix of `S3_METADATA_WRITE_BATCH_SIZE` and
  `S3_METADATA_WRITE_BATCH_WINDOW_MS` under concurrent PUT load, using the
  existing `warp` benchmark setup mentioned in the README.
  - Reasonable Kernel: for the pure write side (io_uring write, no LMDB), find
    where the size/batch-size curve plateaus, similar to Milliscale's
    finding that S3 append latency plateaus 128–512KB before growing
    linearly.
  - For the fdatasync side (once implemented per #2), a bigger batch
    amortizes one fsync over more writes but increases the average wait per
    writer — this is exactly Milliscale's group-size/buffer-size tradeoff
    (Fig. 4 in the paper), just for local NVMe/fdatasync instead of S3.
- Pick values based on data rather than the current defaults, and leave the
  benchmark script/config in the repo so it can be re-run when hardware
  changes (analogous to Milliscale's evaluation methodology in §5.1).

### 4. Optional: apply "restricted" grouping intentionally instead of incidentally
Currently, jobs are grouped into batches purely by arrival time, and then
grouped by engine only at commit time. A more deliberate version, closer to
Milliscale's "restricted decentralized logging" (§4.2):

- Consider whether high-traffic volumes/engines should get their own
  dedicated batch queue (so their batch fill rate and latency profile is
  independent of unrelated low-traffic volumes), while low-traffic ones
  continue to share a queue. This is the same tradeoff Milliscale describes
  as per-group vs. per-thread log buffers — more independence reduces
  cross-tenant latency coupling but increases the number of underlying
  batches/commits.
- This is a bigger structural change than #1–#3 and should only be pursued
  if benchmarking after #1–#3 still shows meaningful head-of-line blocking
  across busy vs. quiet volumes sharing a worker's queue.

## What NOT to import from the paper

- No WAL, no log segmentation into S3 objects, no CSN/DSN dependency
  tracking, no MVCC-based recovery/checkpointing — p2 has no transaction
  ordering to track; each PUT is an independent object write with its own
  LMDB metadata row.
- No S3-Express-specific request-size tuning (512KB–1MB sweet spot) — that
  characteristic is specific to S3's HTTP API latency curve and doesn't
  apply to p2's own local volume files + io_uring + LMDB stack. The
  *methodology* (measure, then size batches to the plateau) is what's being
  reused, not the numbers.

## Suggested order of work

1. Fix per-engine completion resolution in `_write_and_commit_batch` (#1) —
   small, localized, immediately reduces tail latency for multi-tenant
   traffic patterns.
2. Resolve the `S3_VOLUME_FDATASYNC` documentation/behavior mismatch (#2) —
   correctness/durability issue, independent of performance work.
3. Benchmark and retune batch size/window (#3) — data-driven, no code
   structure change required beyond what #1/#2 already touch.
4. Revisit dedicated per-volume queues (#4) only if #1–#3 don't fully resolve
   observed head-of-line blocking under real multi-tenant load.
