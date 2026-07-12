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
                component.pk, volume_pk, exc,
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


async def run_compaction(ctx):
    """Periodic volume compaction — reclaims dead space in sealed .bin files."""
    try:
        from p2.s3.compaction import run_compaction as _compact
        await _compact(ctx)
    except Exception as exc:
        logger.warning("compaction failed (non-fatal): %s", exc)


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
        initial_full_replication,
        run_compaction,
    ]
    cron_jobs = [
        cron(run_expire, second=0),                    # every minute
        cron(run_compaction, minute={0, 15, 30, 45}),  # every 15 minutes
    ]
    redis_settings = RedisSettings.from_dsn(settings.ARQ_REDIS_URL)
    on_startup = on_startup
    on_job_start = on_job_start
    on_job_end = on_job_end
    max_jobs = 50
    job_timeout = 3600  # compaction can take a while for large volumes
    retry_jobs = True
    max_tries = 3
