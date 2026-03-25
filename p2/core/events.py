"""
Event bus implementation using Redis Streams.

Provides publish/consume primitives for blob lifecycle events:
- STREAM_BLOB_POST_SAVE: fired after a blob is saved
- STREAM_BLOB_PAYLOAD_UPDATED: fired after blob payload changes

Event payload schema:
    blob_uuid    (str) hex UUID of the blob
    volume_uuid  (str) hex UUID of the volume
    event_type   (str) "blob_post_save" | "blob_payload_updated"
    timestamp    (str) ISO-8601 UTC timestamp

Delivery guarantees:
- At-least-once via XREADGROUP + XACK (consumer groups)
- Dead-letter after MAX_DELIVERY_ATTEMPTS failed processing attempts
"""

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from django.conf import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stream names
# ---------------------------------------------------------------------------

STREAM_BLOB_POST_SAVE = "p2:events:blob_post_save"
STREAM_BLOB_PAYLOAD_UPDATED = "p2:events:blob_payload_updated"
STREAM_DEAD_LETTER = "p2:events:dead_letter"

MAX_DELIVERY_ATTEMPTS = 5
STREAM_MAX_LEN = 100_000


def _get_redis() -> aioredis.Redis:
    """Return a new async Redis client from the configured REDIS_URL."""
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def make_event(blob_uuid: str, volume_uuid: str, event_type: str) -> dict:
    """Build a well-formed event payload dict."""
    return {
        "blob_uuid": blob_uuid,
        "volume_uuid": volume_uuid,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def publish_event(stream: str, event: dict) -> str:
    """Publish *event* to *stream*. Returns the Redis message ID."""
    r = _get_redis()
    try:
        msg_id = await r.xadd(stream, event, maxlen=STREAM_MAX_LEN, approximate=True)
        logger.debug("published event to %s id=%s type=%s", stream, msg_id, event.get("event_type"))
        return msg_id
    finally:
        await r.aclose()


async def consume_events(stream: str, group: str, consumer: str, handler) -> None:
    """Consume events from *stream* using a consumer group with at-least-once delivery."""
    r = _get_redis()
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    failure_counts: dict[str, int] = {}
    try:
        while True:
            messages = await r.xreadgroup(group, consumer, {stream: ">"}, count=10, block=5000)
            if not messages:
                continue
            for _stream_name, entries in messages:
                for msg_id, data in entries:
                    await _process_message(r, stream, group, msg_id, data, handler, failure_counts)
    finally:
        await r.aclose()


async def _process_message(r, stream, group, msg_id, data, handler, failure_counts):
    try:
        await handler(data)
        await r.xack(stream, group, msg_id)
        failure_counts.pop(msg_id, None)
    except Exception as exc:  # noqa: BLE001
        attempts = failure_counts.get(msg_id, 0) + 1
        failure_counts[msg_id] = attempts
        logger.warning("handler failed for %s on %s (attempt %d/%d): %s",
                       msg_id, stream, attempts, MAX_DELIVERY_ATTEMPTS, exc)
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            await _dead_letter(r, stream, group, msg_id, data, exc)
            failure_counts.pop(msg_id, None)


async def _dead_letter(r, stream, group, msg_id, data, exc):
    payload = {
        **data,
        "original_stream": stream,
        "original_msg_id": msg_id,
        "error": str(exc),
        "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await r.xadd(STREAM_DEAD_LETTER, payload, maxlen=STREAM_MAX_LEN, approximate=True)
        await r.xack(stream, group, msg_id)
        logger.error("dead-lettered message %s from %s: %s", msg_id, stream, exc)
    except aioredis.RedisError as redis_exc:
        logger.error("failed to dead-letter message %s: %s", msg_id, redis_exc)
