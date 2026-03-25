"""p2 S3 Tasks — DEPRECATED (Celery removed).

The ``complete_multipart_upload`` Celery task has been replaced by the async
arq function ``complete_multipart`` in ``p2.core.worker``:

    async def complete_multipart(ctx, upload_id: str, user_pk: int, volume_pk: str, path: str)

It is registered in ``WorkerSettings.functions`` and enqueued via an arq Redis
pool from ``p2/s3/views/multipart.py``.
"""
