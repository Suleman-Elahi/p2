"""
arq worker settings for p2.

Replaces Celery with an async-native task queue backed by Redis.
Run the worker with:
    arq p2.core.worker.WorkerSettings
"""
import logging
import os

from arq import cron
from arq.connections import RedisSettings
from django.conf import settings
from opentelemetry.trace import StatusCode

from p2.core.telemetry import tracer

logger = logging.getLogger(__name__)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")


async def complete_multipart(ctx, upload_id: str, user_pk: int, volume_pk: str, path: str):
    """Assemble multipart upload parts into a final blob via the LSM engine."""
    from p2.core.models import Volume
    from p2.s3.engine import get_engine
    import asyncio, json, os, hashlib
    import aiofiles
    from django.utils.timezone import now
    from p2.core.constants import (ATTR_BLOB_MIME, ATTR_BLOB_SIZE_BYTES,
                                   ATTR_BLOB_IS_FOLDER, ATTR_BLOB_STAT_MTIME,
                                   ATTR_BLOB_STAT_CTIME)

    volume = await Volume.objects.filter(pk=volume_pk).afirst()
    if volume is None:
        logger.error("complete_multipart: volume %s not found", volume_pk)
        return

    engine = await asyncio.to_thread(get_engine, volume)

    # Load part list from redb
    prefix = f"/.multipart/{upload_id}/"
    items = await asyncio.to_thread(engine.list, prefix, None, 10000)
    parts = []
    for key, val in items:
        pnum = key.split('/')[-1]
        if pnum == '_meta':
            continue
        try:
            parts.append((int(pnum), json.loads(val)))
        except Exception:
            pass
    parts.sort(key=lambda x: x[0])

    if not parts:
        logger.error("complete_multipart: no parts found for upload_id=%s", upload_id)
        return

    blob_uuid = __import__('uuid').uuid4().hex
    from p2.core.storage_path import storage_path
    dir_path = storage_path("volumes", volume.uuid.hex, blob_uuid[0:2], blob_uuid[2:4])
    os.makedirs(dir_path, exist_ok=True)
    final_fs_path = os.path.join(dir_path, blob_uuid)
    internal_path = (f"/internal-storage/volumes/{volume.uuid.hex}"
                     f"/{blob_uuid[0:2]}/{blob_uuid[2:4]}/{blob_uuid}")

    total_size = 0
    md5_hash = hashlib.md5()

    async with aiofiles.open(final_fs_path, 'wb') as outfile:
        for _num, part_attr in parts:
            fs_path = part_attr.get('fs_path', '')
            if not fs_path or not os.path.exists(fs_path):
                logger.warning("complete_multipart: missing part file %s", fs_path)
                continue
            async with aiofiles.open(fs_path, 'rb') as infile:
                while True:
                    chunk = await infile.read(1 << 20)
                    if not chunk:
                        break
                    await outfile.write(chunk)
                    md5_hash.update(chunk)
                    total_size += len(chunk)
            try:
                os.remove(fs_path)
            except OSError:
                pass
            await asyncio.to_thread(engine.delete, f"/.multipart/{upload_id}/{_num}")

    final_etag = f"multipart-{len(parts)}-{md5_hash.hexdigest()}"

    meta_str = await asyncio.to_thread(engine.get, f"/.multipart/{upload_id}/_meta")
    m_attr = json.loads(meta_str) if meta_str else {}
    await asyncio.to_thread(engine.delete, f"/.multipart/{upload_id}/_meta")

    await asyncio.to_thread(engine.put, path, json.dumps({
        ATTR_BLOB_MIME: m_attr.get('content_type', 'application/octet-stream'),
        ATTR_BLOB_SIZE_BYTES: str(total_size),
        ATTR_BLOB_IS_FOLDER: False,
        ATTR_BLOB_STAT_MTIME: str(now()),
        ATTR_BLOB_STAT_CTIME: str(now()),
        'blob.p2.io/hash/md5': final_etag,
        'internal_path': internal_path,
    }))
    logger.info("complete_multipart: assembled %d parts → %s (%d bytes)",
                len(parts), path, total_size)


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
            logger.error(
                "initial_full_replication: component %s failed for volume %s: %s",
                component.pk, volume_pk, exc
            )


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
    import django
    django.setup()
    from p2.core.consumers import start_consumers
    from p2.core.telemetry import setup_telemetry
    setup_telemetry()
    ctx["consumer_tasks"] = await start_consumers()


async def on_job_start(ctx):
    """Create an OTel span when an arq job begins."""
    job_name = ctx.get("job_name", "unknown")
    span = tracer.start_span(f"arq.job.{job_name}", attributes={"arq.job_name": job_name})
    ctx["_otel_span"] = span


async def on_job_end(ctx):
    """End the OTel span when an arq job finishes."""
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
