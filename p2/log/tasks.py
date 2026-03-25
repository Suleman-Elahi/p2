"""p2 log tasks — DEPRECATED (Celery removed).

write_log_record was a Celery task. Log writing is now handled synchronously
in p2.log.adaptor or via the async event bus.
"""
