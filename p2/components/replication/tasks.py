"""
p2 replication component tasks — DEPRECATED (Celery removed).

Async arq implementations are in ``p2.core.worker``:

    - ``replicate_metadata(ctx, blob_pk)``
    - ``replicate_payload(ctx, blob_pk)``
    - ``replicate_delete(ctx, blob_pk)``
    - ``initial_full_replication(ctx, volume_pk)``

These functions are registered in ``WorkerSettings.functions`` and are
enqueued via an arq Redis pool (see ``p2/components/replication/signals.py``).
"""
