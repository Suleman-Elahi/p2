"""Replication signals — arq-based task enqueueing (Celery removed)."""
import logging

from asgiref.sync import async_to_sync
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from p2.components.replication.constants import TAG_REPLICATION_OFFSET
from p2.components.replication.controller import ReplicationController
from p2.core.models import Blob, Component
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


@receiver(pre_delete, sender=Blob)
def blob_pre_delete(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Enqueue replicate_delete arq task when a blob is about to be deleted."""
    controller_path = class_to_path(ReplicationController)
    component = instance.volume.component_set.filter(
        controller_path=controller_path, enabled=True
    ).first()
    if not component:
        return
    countdown = int(component.tags.get(TAG_REPLICATION_OFFSET, 0))
    try:
        async_to_sync(_enqueue)("replicate_delete", str(instance.pk), countdown=countdown)
    except Exception as exc:  # noqa: BLE001
        logger.error("blob_pre_delete: failed to enqueue replicate_delete for blob %s: %s", instance.pk, exc)


@receiver(post_save, sender=Component)
def component_post_save(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Trigger initial full replication after a ReplicationController component is saved."""
    if instance.controller_path != class_to_path(ReplicationController):
        return
    try:
        async_to_sync(_enqueue)("initial_full_replication", str(instance.volume.pk))
    except Exception as exc:  # noqa: BLE001
        logger.error("component_post_save: failed to enqueue initial_full_replication for volume %s: %s",
                     instance.volume.pk, exc)
