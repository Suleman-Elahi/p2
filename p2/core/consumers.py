"""
Event consumers for blob lifecycle handlers.

All handlers operate against the redb LSM metadata engine and the
physical file store — no Django ORM Blob model required.
"""

import asyncio
import json
import logging
import os

from p2.core.events import (
    STREAM_BLOB_PAYLOAD_UPDATED,
    STREAM_BLOB_POST_SAVE,
    consume_events,
)

logger = logging.getLogger(__name__)

GROUP_HASH = "p2:consumers:hash"
GROUP_REPLICATION_METADATA = "p2:consumers:replication_metadata"
GROUP_REPLICATION_PAYLOAD = "p2:consumers:replication_payload"
GROUP_EXPIRY = "p2:consumers:expiry"
GROUP_IMAGE_EXIF = "p2:consumers:image_exif"
CONSUMER_NAME = "worker-1"


async def handle_hash_computation(data: dict) -> None:
    """Verify SHA256 is present for a blob.

    The PUT path already computes SHA256 inline and stores it in redb.
    This handler is a no-op — the web process owns the redb lock.
    If SHA256 is missing (e.g. blobs written by external tools), a future
    re-index pass will backfill it.
    """
    logger.debug("handle_hash_computation: SHA256 computed inline by PUT handler, skipping %s",
                 data.get("blob_path"))


async def handle_replication_metadata(data: dict) -> None:
    """Stub: full replication requires a configured ReplicationController component."""
    logger.debug("handle_replication_metadata: replication not configured, skipping %s", data.get("blob_path"))


async def handle_replication_payload(data: dict) -> None:
    """Stub: full replication requires a configured ReplicationController component."""
    logger.debug("handle_replication_payload: replication not configured, skipping %s", data.get("blob_path"))


async def handle_expiry_scheduling(data: dict) -> None:
    """Check if the volume has an expiry policy and schedule deletion if needed."""
    volume_uuid = data.get("volume_uuid", "")
    blob_path = data.get("blob_path", "")
    if not volume_uuid or not blob_path:
        return
    try:
        from p2.core.models import Volume
        volume = await Volume.objects.filter(uuid=volume_uuid).afirst()
        if volume is None:
            return
        # Check for an ExpiryController component
        from p2.lib.reflection import class_to_path
        from p2.components.expire.controller import ExpiryController
        controller_path = class_to_path(ExpiryController)
        component = await volume.component_set.filter(
            controller_path=controller_path, enabled=True
        ).afirst()
        if component is None:
            return
        # Delegate to the controller — it reads TTL from component settings
        await asyncio.to_thread(component.controller.schedule_expiry, blob_path, volume)
    except Exception as exc:
        logger.warning("handle_expiry_scheduling failed for %s/%s: %s", volume_uuid, blob_path, exc)
        raise


async def handle_image_exif(data: dict) -> None:
    """Extract EXIF metadata from images.

    Derives the filesystem path from the event payload (blob_uuid + volume_uuid)
    without opening redb — the web process holds the exclusive redb lock.
    EXIF data is published back as a follow-up event for the web process to store.
    """
    volume_uuid = data.get("volume_uuid", "")
    blob_uuid = data.get("blob_uuid", "")
    blob_path = data.get("blob_path", "")
    if not volume_uuid or not blob_uuid:
        return

    # Skip non-image files early using MIME from the event (set by PUT handler)
    mime = data.get("mime", "") or data.get("blob.p2.io/mime", "")
    if mime and not mime.startswith("image/"):
        return

    # Reconstruct the filesystem path from the blob UUID (matches PUT handler layout)
    fs_path = (f"/storage/volumes/{volume_uuid}"
               f"/{blob_uuid[0:2]}/{blob_uuid[2:4]}/{blob_uuid}")

    if not os.path.exists(fs_path):
        logger.debug("image_exif: file not found at %s, skipping", fs_path)
        return

    try:
        from PIL import Image, ExifTags
        img = await asyncio.to_thread(Image.open, fs_path)
        exif_data = getattr(img, "_getexif", lambda: None)() or {}
        exif_tags = {}
        for tag_id, value in exif_data.items():
            tag = ExifTags.TAGS.get(tag_id, str(tag_id))
            if isinstance(value, (str, int, float)):
                exif_tags[f"exif.{tag.lower()}"] = str(value)
        if exif_tags:
            logger.debug("image_exif: extracted %d EXIF tags for %s", len(exif_tags), blob_path)
            # Publish EXIF tags back so the web process can merge them into redb
            from p2.core.events import STREAM_BLOB_PAYLOAD_UPDATED, make_event, publish_event
            event = make_event(blob_uuid=blob_uuid, volume_uuid=volume_uuid,
                               event_type="blob_exif_extracted")
            event["blob_path"] = blob_path
            event["exif_json"] = json.dumps(exif_tags)
            await publish_event(STREAM_BLOB_PAYLOAD_UPDATED, event)
    except ImportError:
        logger.debug("image_exif: Pillow not installed, skipping")
    except Exception as exc:
        logger.debug("image_exif: could not extract EXIF from %s: %s", blob_path, exc)


async def start_consumers() -> list:
    """Start all event consumers as asyncio tasks."""
    tasks = [
        asyncio.create_task(
            consume_events(STREAM_BLOB_POST_SAVE, GROUP_HASH, CONSUMER_NAME, handle_hash_computation),
            name="consumer:hash",
        ),
        asyncio.create_task(
            consume_events(STREAM_BLOB_POST_SAVE, GROUP_REPLICATION_METADATA, CONSUMER_NAME, handle_replication_metadata),
            name="consumer:replication_metadata",
        ),
        asyncio.create_task(
            consume_events(STREAM_BLOB_PAYLOAD_UPDATED, GROUP_REPLICATION_PAYLOAD, CONSUMER_NAME, handle_replication_payload),
            name="consumer:replication_payload",
        ),
        asyncio.create_task(
            consume_events(STREAM_BLOB_POST_SAVE, GROUP_EXPIRY, CONSUMER_NAME, handle_expiry_scheduling),
            name="consumer:expiry",
        ),
        asyncio.create_task(
            consume_events(STREAM_BLOB_POST_SAVE, GROUP_IMAGE_EXIF, CONSUMER_NAME, handle_image_exif),
            name="consumer:image_exif",
        ),
    ]
    logger.info("started %d event consumers", len(tasks))
    return tasks
