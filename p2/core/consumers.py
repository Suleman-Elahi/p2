"""
Event consumers for blob lifecycle component handlers.

Each consumer function receives a data dict with:
    blob_uuid    (str) hex UUID of the blob
    volume_uuid  (str) hex UUID of the volume
    event_type   (str) "blob_post_save" | "blob_payload_updated"
    timestamp    (str) ISO-8601 UTC timestamp

Consumers are wired to Redis Streams via consumer groups and started
as asyncio tasks by start_consumers().
"""

import asyncio
import hashlib
import logging

from p2.core.events import (
    STREAM_BLOB_PAYLOAD_UPDATED,
    STREAM_BLOB_POST_SAVE,
    consume_events,
)

logger = logging.getLogger(__name__)

# Consumer group names
GROUP_HASH = "p2:consumers:hash"
GROUP_REPLICATION_METADATA = "p2:consumers:replication_metadata"
GROUP_REPLICATION_PAYLOAD = "p2:consumers:replication_payload"
GROUP_EXPIRY = "p2:consumers:expiry"
GROUP_IMAGE_EXIF = "p2:consumers:image_exif"

# Consumer instance names (one worker process = one consumer per group)
CONSUMER_NAME = "worker-1"


async def handle_hash_computation(data: dict) -> None:
    """Compute MD5 + SHA256 hashes on blob payload and store in blob.attributes."""
    from p2.core.constants import ATTR_BLOB_HASH_MD5, ATTR_BLOB_HASH_SHA256
    from p2.core.models import Blob

    blob_uuid = data.get("blob_uuid")
    try:
        blob = await Blob.objects.filter(uuid=blob_uuid).afirst()
        if blob is None:
            logger.warning("handle_hash_computation: blob %s not found", blob_uuid)
            return

        storage_controller = blob.volume.storage.controller
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()

        if hasattr(storage_controller, "get_read_stream"):
            # Async storage backend
            async for chunk in storage_controller.get_read_stream(blob):
                md5.update(chunk)
                sha256.update(chunk)
        else:
            # Sync fallback: read via file-like interface
            blob.seek(0)
            while True:
                chunk = blob.read(65536)
                if not chunk:
                    break
                md5.update(chunk)
                sha256.update(chunk)

        blob.attributes[ATTR_BLOB_HASH_MD5] = md5.hexdigest()
        blob.attributes[ATTR_BLOB_HASH_SHA256] = sha256.hexdigest()
        await blob.asave(update_fields=["attributes"])
        logger.debug("handle_hash_computation: updated hashes for blob %s", blob_uuid)
    except Exception as exc:  # noqa: BLE001
        logger.error("handle_hash_computation: error processing blob %s: %s", blob_uuid, exc)


async def handle_replication_metadata(data: dict) -> None:
    """Trigger replication metadata update for all replication components on the volume."""
    from p2.components.replication.controller import ReplicationController
    from p2.core.models import Blob
    from p2.lib.reflection import class_to_path

    blob_uuid = data.get("blob_uuid")
    try:
        blob = await Blob.objects.filter(uuid=blob_uuid).select_related(
            "volume", "volume__storage"
        ).afirst()
        if blob is None:
            logger.warning("handle_replication_metadata: blob %s not found", blob_uuid)
            return

        controller_path = class_to_path(ReplicationController)
        components = blob.volume.component_set.filter(
            controller_path=controller_path, enabled=True
        )
        async for component in components.aiterator():
            try:
                target_blob = component.controller.metadata_update(blob)
                await target_blob.asave()
                logger.debug(
                    "handle_replication_metadata: replicated metadata for blob %s via component %s",
                    blob_uuid,
                    component.pk,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "handle_replication_metadata: component %s failed for blob %s: %s",
                    component.pk,
                    blob_uuid,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "handle_replication_metadata: error processing blob %s: %s", blob_uuid, exc
        )


async def handle_replication_payload(data: dict) -> None:
    """Trigger replication payload update for all replication components on the volume."""
    from p2.components.replication.controller import ReplicationController
    from p2.core.models import Blob
    from p2.lib.reflection import class_to_path

    blob_uuid = data.get("blob_uuid")
    try:
        blob = await Blob.objects.filter(uuid=blob_uuid).select_related(
            "volume", "volume__storage"
        ).afirst()
        if blob is None:
            logger.warning("handle_replication_payload: blob %s not found", blob_uuid)
            return

        controller_path = class_to_path(ReplicationController)
        components = blob.volume.component_set.filter(
            controller_path=controller_path, enabled=True
        )
        async for component in components.aiterator():
            try:
                target_blob = component.controller.payload_update(blob)
                await target_blob.asave()
                logger.debug(
                    "handle_replication_payload: replicated payload for blob %s via component %s",
                    blob_uuid,
                    component.pk,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "handle_replication_payload: component %s failed for blob %s: %s",
                    component.pk,
                    blob_uuid,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "handle_replication_payload: error processing blob %s: %s", blob_uuid, exc
        )


async def handle_expiry_scheduling(data: dict) -> None:
    """Schedule expiry for blobs with expiry components on the volume."""
    from p2.components.expire.constants import TAG_EXPIRE_DATE
    from p2.components.expire.controller import ExpiryController
    from p2.core.models import Blob
    from p2.lib.reflection import class_to_path

    blob_uuid = data.get("blob_uuid")
    try:
        blob = await Blob.objects.filter(uuid=blob_uuid).select_related("volume").afirst()
        if blob is None:
            logger.warning("handle_expiry_scheduling: blob %s not found", blob_uuid)
            return

        # Only act if this blob has an expiry date tag set
        if TAG_EXPIRE_DATE not in blob.tags:
            return

        controller_path = class_to_path(ExpiryController)
        components = blob.volume.component_set.filter(
            controller_path=controller_path, enabled=True
        )
        has_component = await components.aexists()
        if not has_component:
            return

        logger.debug(
            "handle_expiry_scheduling: blob %s has expiry tag, expiry component active",
            blob_uuid,
        )
        # The actual expiry sweep runs as a periodic arq cron job (run_expire).
        # This handler confirms the blob is eligible and logs it for observability.
        # If a task queue pool is available, enqueue an immediate expiry check.
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            from django.conf import settings

            pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            await pool.enqueue_job("run_expire")
            await pool.aclose()
            logger.debug(
                "handle_expiry_scheduling: enqueued run_expire for blob %s", blob_uuid
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "handle_expiry_scheduling: could not enqueue run_expire (will rely on cron): %s",
                exc,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "handle_expiry_scheduling: error processing blob %s: %s", blob_uuid, exc
        )


async def handle_image_exif(data: dict) -> None:
    """Extract EXIF data from image blobs and store in blob.attributes."""
    from p2.components.image.constants import TAG_IMAGE_EXIF_TAGS
    from p2.components.image.controller import ImageController
    from p2.core.models import Blob
    from p2.lib.reflection import class_to_path

    blob_uuid = data.get("blob_uuid")
    try:
        blob = await Blob.objects.filter(uuid=blob_uuid).select_related("volume").afirst()
        if blob is None:
            logger.warning("handle_image_exif: blob %s not found", blob_uuid)
            return

        controller_path = class_to_path(ImageController)
        components = blob.volume.component_set.filter(
            controller_path=controller_path, enabled=True
        )
        async for component in components.aiterator():
            try:
                # ImageController.handle() is synchronous (uses PIL); run in thread executor
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, component.controller.handle, blob)
                logger.debug(
                    "handle_image_exif: extracted EXIF for blob %s via component %s",
                    blob_uuid,
                    component.pk,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "handle_image_exif: component %s failed for blob %s: %s",
                    component.pk,
                    blob_uuid,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("handle_image_exif: error processing blob %s: %s", blob_uuid, exc)


async def start_consumers() -> None:
    """Start all event consumers as asyncio tasks.

    Call this from the worker process startup (e.g. arq WorkerSettings.on_startup).
    Each consumer runs in an infinite loop, reading from its assigned stream and group.
    """
    tasks = [
        asyncio.create_task(
            consume_events(
                stream=STREAM_BLOB_PAYLOAD_UPDATED,
                group=GROUP_HASH,
                consumer=CONSUMER_NAME,
                handler=handle_hash_computation,
            ),
            name="consumer:hash",
        ),
        asyncio.create_task(
            consume_events(
                stream=STREAM_BLOB_POST_SAVE,
                group=GROUP_REPLICATION_METADATA,
                consumer=CONSUMER_NAME,
                handler=handle_replication_metadata,
            ),
            name="consumer:replication_metadata",
        ),
        asyncio.create_task(
            consume_events(
                stream=STREAM_BLOB_PAYLOAD_UPDATED,
                group=GROUP_REPLICATION_PAYLOAD,
                consumer=CONSUMER_NAME,
                handler=handle_replication_payload,
            ),
            name="consumer:replication_payload",
        ),
        asyncio.create_task(
            consume_events(
                stream=STREAM_BLOB_POST_SAVE,
                group=GROUP_EXPIRY,
                consumer=CONSUMER_NAME,
                handler=handle_expiry_scheduling,
            ),
            name="consumer:expiry",
        ),
        asyncio.create_task(
            consume_events(
                stream=STREAM_BLOB_PAYLOAD_UPDATED,
                group=GROUP_IMAGE_EXIF,
                consumer=CONSUMER_NAME,
                handler=handle_image_exif,
            ),
            name="consumer:image_exif",
        ),
    ]
    logger.info("started %d event consumers", len(tasks))
    return tasks
