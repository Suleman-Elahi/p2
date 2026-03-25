"""
expire signals — DEPRECATED (Celery removed).

The expiry sweep now runs as a periodic arq cron job (every 60 seconds)
via ``p2.core.worker.run_expire``. No signal-based scheduling is needed —
the cron job handles all expiry automatically.
"""
