"""
expiry tasks — DEPRECATED (Celery removed).

The expiry sweep has been migrated to an arq async cron job.
See ``p2.core.worker.run_expire`` which runs every 60 seconds via
``WorkerSettings.cron_jobs``.
"""
