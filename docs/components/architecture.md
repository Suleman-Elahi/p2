# Component System Architecture

## Overview

Components are pluggable feature modules that attach to a `Volume` and hook into the blob lifecycle. Each component is an instance of the `Component` model pointing to a `ComponentController` class. A volume can have at most one instance of each component type.

```
Volume
  └── Component (enabled=True, controller_path="p2.components.quota.controller.QuotaController")
        └── ComponentController instance
              └── tags (configuration key/value pairs)
```

Components are opt-in per volume. If a component is not configured on a volume, it has zero effect on blobs in that volume.

---

## Base Classes

```
Controller
  └── ComponentController
        ├── QuotaController
        ├── ExpiryController
        ├── ReplicationController
        ├── PublicAccessController
        └── ImageController
```

`Controller` provides:
- `instance` — the model instance (Component or Storage)
- `tags` — shortcut to `instance.tags` (key/value config dict)
- `get_required_tags()` — list of tags that must be set for the controller to function

`ComponentController` adds:
- `volume` — the Volume this component is attached to

Controllers are instantiated lazily via `Component.controller` property using Python reflection (`path_to_class`).

---

## Lifecycle Hooks

Components integrate with the blob lifecycle through Django signals. The core signals are:

| Signal | Fired when |
|---|---|
| `BLOB_PRE_SAVE` | Before `Blob.save()` completes (synchronous) |
| `BLOB_POST_SAVE` | After `Blob.save()` completes (synchronous) |
| `BLOB_PAYLOAD_UPDATED` | After blob binary data is written and committed (async via Celery) |

Each component's `signals.py` registers receivers on these signals. The receiver checks whether the blob's volume has that component enabled, then calls the controller method.

```
Blob.save()
  │
  ├── BLOB_PRE_SAVE  ──► QuotaController.before_save()   [blocks if over quota]
  │
  ├── (commit to storage)
  │
  ├── BLOB_POST_SAVE ──► ReplicationController → replicate_metadata_update_task (Celery)
  │                  ──► PublicAccessController.add_permissions()
  │                  ──► ExpiryController → run_expire.apply_async(eta=expire_date)
  │
  └── BLOB_PAYLOAD_UPDATED (dispatched via Celery signal_marshall task)
        ──► ReplicationController → replicate_payload_update_task (Celery)
        ──► ImageController.handle()  [EXIF extraction]
        ──► blob_payload_hash()       [MD5/SHA1/SHA256/SHA384/SHA512 computation]
```

---

## Components

### Quota

**Purpose:** Enforce a maximum storage size on a volume. Prevents writes that would push the volume over a configured threshold.

**Controller:** `p2.components.quota.controller.QuotaController`

**Hook:** `BLOB_PRE_SAVE` (synchronous — can abort the save)

**Configuration tags:**

| Tag | Description |
|---|---|
| `component.p2.io/quota/threshold` | Max bytes allowed in the volume (integer as string) |
| `component.p2.io/quota/action` | What to do when exceeded: `nothing`, `block`, `e-mail` |

**Flow:**
1. `BLOB_PRE_SAVE` fires before the blob is committed
2. `before_save()` computes `volume.space_used + new_blob_size`
3. If over threshold, `do_action()` is called
   - `nothing` — logs a warning, save continues
   - `block` — raises `QuotaExceededException`, save is aborted
   - `e-mail` — not yet implemented (TODO)

**Notes:**
- `space_used` is computed via a DB aggregate over all blob `attributes[blob.p2.io/size/bytes]`
- `quota_percentage` is available for UI display
- The check runs synchronously, so `block` reliably prevents the write

---

### Expiry

**Purpose:** Automatically delete blobs after a Unix timestamp stored on the blob's tags.

**Controller:** `p2.components.expire.controller.ExpiryController`

**Hooks:**
- `BLOB_POST_SAVE` — schedules a Celery task at the exact expiry time
- Celery beat — `run_expire` task runs every 60 seconds as a fallback sweep

**Configuration tags:** None on the component itself. Expiry is configured per-blob:

| Tag | Description |
|---|---|
| `component.p2.io/expiry/date` | Unix timestamp (integer) after which the blob is deleted |

**Flow:**
1. When a blob is saved with `component.p2.io/expiry/date` in its tags, `blob_post_save_expire` schedules `run_expire` via `apply_async(eta=date)`
2. At the scheduled time, `run_expire` iterates all volumes with `ExpiryController` enabled
3. For each blob with an expiry tag, if `time() >= expire_date`, the blob is deleted
4. The periodic 60-second sweep catches any blobs that were missed (e.g. worker downtime)

**Notes:**
- Expiry is also used internally by the multipart upload system — part blobs get a 24-hour expiry tag (`DEFAULT_BLOB_EXPIRY = 86400`)
- Deletion triggers `pre_delete` signal, which cascades to replication delete if configured

---

### Replication

**Purpose:** Mirror blobs 1:1 from a source volume to a target volume, including metadata and binary payload. Keeps the target in sync with creates, updates, and deletes.

**Controller:** `p2.components.replication.controller.ReplicationController`

**Hooks:**
- `BLOB_POST_SAVE` → `replicate_metadata_update_task` (Celery)
- `BLOB_PAYLOAD_UPDATED` → `replicate_payload_update_task` (Celery)
- `pre_delete` on Blob → `replicate_delete_task` (Celery)
- `post_save` on Component → `initial_full_replication` (Celery, runs once on setup)

**Configuration tags:**

| Tag | Description |
|---|---|
| `component.p2.io/replication/target` | UUID of the target Volume |
| `component.p2.io/replication/offset` | Delay in seconds before replication tasks run (countdown) |
| `component.p2.io/replication/ignore_if` | (defined, not yet implemented) |

**Flow:**

Initial setup:
1. Component is saved → `component_post_save` signal fires
2. `initial_full_replication` task runs, iterating all blobs in the source volume
3. Each blob gets metadata and payload copied to the target volume

Ongoing sync:
1. Blob metadata saved → `replicate_metadata_update_task` copies path, prefix, attributes, tags to target blob
2. Blob payload updated → `replicate_payload_update_task` streams binary data to target blob via `copyfileobj`
3. Blob deleted → `replicate_delete_task` deletes the corresponding target blob

Target blob identity:
- Target blobs store `blob.p2.io/replication/source_uuid` in their attributes
- This is used to find the correct target blob on subsequent updates without relying on path matching

**Notes:**
- All replication tasks are async (Celery). There is a replication lag equal to task queue latency plus the optional `offset` countdown
- The target volume can use a different storage backend than the source — this is the primary use case for cross-backend replication
- Circular replication (A → B → A) is not guarded against

---

### Public Access

**Purpose:** Make all blobs in a volume readable by unauthenticated (anonymous) users by assigning object-level view permissions.

**Controller:** `p2.components.public_access.controller.PublicAccessController`

**Hook:** `BLOB_POST_SAVE` (synchronous)

**Configuration tags:** None

**Flow:**
1. Any blob saved to a volume with this component enabled triggers `blob_post_save_perms`
2. `add_permissions()` calls `assign_perm('p2_core.view_blob', get_anonymous_user(), blob)`
3. The anonymous user can now retrieve the blob via the S3 GET or serve endpoints without authentication

**Notes:**
- Uses `django-guardian` for object-level permissions
- Only grants `view_blob` — anonymous users cannot modify or delete
- Permissions are assigned per-blob on save, so blobs uploaded before the component was enabled are not retroactively made public

---

### Image

**Purpose:** Extract EXIF metadata from image blobs and store it as blob attributes.

**Controller:** `p2.components.image.controller.ImageController`

**Hook:** `BLOB_PAYLOAD_UPDATED` (runs after binary data is committed)

**Configuration tags:**

| Tag | Description |
|---|---|
| `component.p2.io/image/exif_tags` | List of EXIF tag names to extract (defaults to a built-in set) |

**Default extracted tags:** `ImageWidth`, `ImageHeight`, `Compression`, `Orientation`, `Model`, `Software`

**Flow:**
1. Blob payload is written and committed to storage
2. `BLOB_PAYLOAD_UPDATED` fires → `payload_updated_exif` calls `ImageController.handle(blob)`
3. Pillow opens the blob as an image
4. All existing `blob.p2.io/exif/*` attributes are cleared (prevents stale keys)
5. `_getexif()` is called; each numeric EXIF key is resolved to a name via `PIL.ExifTags.TAGS`
6. Only string values and tags in the allowed list are kept
7. Attributes are stored as `blob.p2.io/exif/<TagName>` and the blob is saved

**Notes:**
- Non-image blobs are silently skipped (`IOError` is caught)
- Only string EXIF values are stored — numeric values (e.g. raw GPS coordinates) are ignored
- The blob is re-saved after EXIF extraction, which re-fires `BLOB_POST_SAVE` signals

---

## Adding a New Component

1. Create a new app under `p2/components/<name>/`
2. Subclass `ComponentController` from `p2.core.components.base`
3. Set `template_name` and `form_class`
4. Implement lifecycle methods (`before_save`, `handle`, etc.)
5. Register signal receivers in `signals.py` — check `blob.volume.component(YourController)` before acting
6. Register the app in Django settings and add the controller path to the `component.controllers` entry point group so `COMPONENT_MANAGER` discovers it

## Architecture

Clients (AWS CLI, boto3, SDKs)
        │
        ▼
  S3 API Layer (p2/s3/)
  ├── AWS v4 signature auth (header + querystring)
  ├── Bucket operations (GET list, PUT create, DELETE)
  ├── Object operations (GET, PUT, DELETE, HEAD)
  └── Multipart upload (initiate, upload parts, complete)
        │
        ▼
  Core Storage Engine (p2/core/)
  ├── Volume  ──── logical bucket/namespace
  ├── Blob    ──── individual object with path, attributes (JSON), tags
  └── Storage ──── backend config (local or S3)
        │
        ├── Local Storage Controller  → filesystem (uuid-sharded paths)
        └── S3 Storage Controller     → boto3 → any S3-compatible backend
        │
        ▼
  Component System (p2/components/)
  ├── Quota      → block writes when threshold exceeded
  ├── Expiry     → auto-delete blobs after timestamp
  ├── Replication → 1:1 sync between volumes
  ├── Public Access → assign anonymous view permissions
  └── Image      → EXIF/dimension extraction
        │
        ▼
  Supporting Services
  ├── REST API (p2/api/)     → DRF + JWT for management
  ├── gRPC Serve (p2/serve/) → URL-regex → blob routing for web serving
  ├── Celery + Redis         → async tasks (hashing, multipart assembly)
  └── PostgreSQL             → metadata, permissions (django-guardian)