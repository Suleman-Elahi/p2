"""
arq worker settings for p2.

Replaces Celery with an async-native task queue backed by Redis.
Run the worker with:
    arq p2.core.worker.WorkerSettings
"""
import logging

from arq import cron
from arq.connections import RedisSettings
from django.conf import settings
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from p2.core.telemetry import tracer

logger = logging.getLogger(__name__)


async def replicate_metadata(ctx, blob_pk: str):
    """Replicate blob metadata to target volume."""
    from p2.components.replication.controller import ReplicationController
    from p2.core.models import Blob
    from p2.lib.reflection import class_to_path

    blob = await Blob.objects.filter(pk=blob_pk).select_related("volume", "volume__storage").afirst()
    if blob is None:
        logger.warning("replicate_metadata: blob %s not found", blob_pk)
        return
    controller_path = class_to_path(ReplicationController)
    async for component in blob.volume.component_set.filter(
        controller_path=controller_path, enabled=True
    ).aiterator():
        try:
            target_blob = component.controller.metadata_update(blob)
            await target_blob.asave()
        except Exception as exc:  # noqa: BLE001
            logger.error("replicate_metadata: component %s failed for blob %s: %s", component.pk, blob_pk, exc)


async def replicate_payload(ctx, blob_pk: str):
    """Replicate blob payload to target volume."""
    from p2.components.replication.controller import ReplicationController
    from p2.core.models import Blob
    from p2.lib.reflection import class_to_path

    blob = await Blob.objects.filter(pk=blob_pk).select_related("volume", "volume__storage").afirst()
    if blob is None:
        logger.warning("replicate_payload: blob %s not found", blob_pk)
        return
    controller_path = class_to_path(ReplicationController)
    async for component in blob.volume.component_set.filter(
        controller_path=controller_path, enabled=True
    ).aiterator():
        try:
            target_blob = component.controller.payload_update(blob)
            await target_blob.asave()
        except Exception as exc:  # noqa: BLE001
            logger.error("replicate_payload: component %s failed for blob %s: %s", component.pk, blob_pk, exc)


async def replicate_delete(ctx, blob_pk: str):
    """Delete replicated blob from target volume."""
    from p2.components.replication.controller import ReplicationController
    from p2.core.models import Blob
    from p2.lib.reflection import class_to_path

    blob = await Blob.objects.filter(pk=blob_pk).select_related("volume", "volume__storage").afirst()
    if blob is None:
        logger.warning("replicate_delete: blob %s not found", blob_pk)
        return
    controller_path = class_to_path(ReplicationController)
    async for component in blob.volume.component_set.filter(
        controller_path=controller_path, enabled=True
    ).aiterator():
        try:
            component.controller.delete(blob)
        except Exception as exc:  # noqa: BLE001
            logger.error("replicate_delete: component %s failed for blob %s: %s", component.pk, blob_pk, exc)


async def complete_multipart(ctx, upload_id: str, user_pk: int, volume_pk: str, path: str):
    """Assemble multipart upload parts into a final blob."""
    from p2.core.models import Blob, Volume

    volume = await Volume.objects.filter(pk=volume_pk).select_related("storage").afirst()
    if volume is None:
        logger.error("complete_multipart: volume %s not found", volume_pk)
        return
    blob = await Blob.objects.filter(volume=volume, path=path).select_related("volume", "volume__storage").afirst()
    if blob is None:
        logger.error("complete_multipart: blob not found for volume=%s path=%s", volume_pk, path)
        return
    storage_controller = volume.storage.controller
    if hasattr(storage_controller, "complete_multipart_upload"):
        await storage_controller.complete_multipart_upload(blob, upload_id)
    else:
        logger.warning("complete_multipart: storage controller does not support complete_multipart_upload")


async def initial_full_replication(ctx, volume_pk: str):
    """Run initial full replication after a ReplicationController component is configured."""
    from p2.components.replication.controller import ReplicationController
    from p2.core.models import Volume
    from p2.lib.reflection import class_to_path

    volume = await Volume.objects.filter(pk=volume_pk).select_related("storage").afirst()
    if volume is None:
        logger.error("initial_full_replication: volume %s not found", volume_pk)
        return
    controller_path = class_to_path(ReplicationController)
    async for component in volume.component_set.filter(
        controller_path=controller_path, enabled=True
    ).aiterator():
        try:
            component.controller.full_replication(volume)
        except Exception as exc:  # noqa: BLE001
            logger.error("initial_full_replication: component %s failed for volume %s: %s", component.pk, volume_pk, exc)


async def run_expire(ctx):
    """Periodic expiry sweep — runs every 60 seconds via cron."""
    from p2.components.expire.controller import ExpiryController
    from p2.core.models import Component
    from p2.lib.reflection import class_to_path

    controller_path = class_to_path(ExpiryController)
    async for component in Component.objects.filter(
        controller_path=controller_path, enabled=True
    ).select_related("volume").aiterator():
        try:
            component.controller.expire_volume(component.volume)
        except Exception as exc:  # noqa: BLE001
            logger.error("run_expire: error expiring volume %s: %s", component.volume.pk, exc)


async def on_startup(ctx):
    """Start Redis Stream event consumers and OTel when the worker process starts."""
    from p2.core.consumers import start_consumers
    from p2.core.telemetry import setup_telemetry
    setup_telemetry()
    ctx["consumer_tasks"] = await start_consumers()


async def on_job_start(ctx):
    """Create an OTel span when an arq job begins. Satisfies Requirement 9.4."""
    job_name = ctx.get("job_name", "unknown")
    span = tracer.start_span(f"arq.job.{job_name}", attributes={"arq.job_name": job_name})
    ctx["_otel_span"] = span


async def on_job_end(ctx):
    """End the OTel span when an arq job finishes. Satisfies Requirement 9.4."""
    span = ctx.pop("_otel_span", None)
    if span is None:
        return
    job_name = ctx.get("job_name", "unknown")
    result = ctx.get("result")
    if isinstance(result, Exception):
        span.set_status(StatusCode.ERROR, str(result))
        span.record_exception(result)
    else:
        span.set_status(StatusCode.OK)
    span.set_attribute("arq.job_name", job_name)
    span.end()


class WorkerSettings:
    """arq WorkerSettings — run with `arq p2.core.worker.WorkerSettings`."""

    functions = [
        replicate_metadata,
        replicate_payload,
        replicate_delete,
        complete_multipart,
        initial_full_replication,
    ]
    cron_jobs = [cron(run_expire, second=0)]  # every minute
    redis_settings = RedisSettings.from_dsn(settings.ARQ_REDIS_URL)
    on_startup = on_startup
    on_job_start = on_job_start
    on_job_end = on_job_end
    max_jobs = 50
    job_timeout = 300
    retry_jobs = True
    max_tries = 5
