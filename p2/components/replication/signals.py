"""Replication signals — arq-based task enqueueing (Celery removed).

NOTE: The pre_delete signal for Blob has been removed since the Blob model
is gone.  The Component post_save signal is kept — it triggers an initial
full replication job when a ReplicationController component is saved.
"""
import logging

from asgiref.sync import async_to_sync
from django.db.models.signals import post_save
from django.dispatch import receiver

from p2.components.replication.controller import ReplicationController
from p2.core.models import Component
from p2.lib.reflection import class_to_path

logger = logging.getLogger(__name__)


async def _enqueue(job_name: str, *args, countdown: int = 0):
    """Enqueue an arq job, optionally with a defer-by countdown (seconds)."""
    import datetime
    from arq import create_pool
    from arq.connections import RedisSettings
    from django.conf import settings

    pool = await create_pool(RedisSettings.from_dsn(settings.ARQ_REDIS_URL))
    try:
        kwargs = {}
        if countdown:
            kwargs["_defer_by"] = datetime.timedelta(seconds=countdown)
        await pool.enqueue_job(job_name, *args, **kwargs)
    finally:
        await pool.aclose()


@receiver(post_save, sender=Component)
def component_post_save(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Trigger initial full replication after a ReplicationController component is saved."""
    if instance.controller_path != class_to_path(ReplicationController):
        return
    try:
        async_to_sync(_enqueue)("initial_full_replication", str(instance.volume.pk))
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "component_post_save: failed to enqueue initial_full_replication for volume %s: %s",
            instance.volume.pk, exc
        )
