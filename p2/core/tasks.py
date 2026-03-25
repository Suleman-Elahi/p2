"""p2 core tasks — DEPRECATED (Celery removed).

signal_marshall has been removed. Blob lifecycle events are now published
via Redis Streams (p2.core.events) and consumed by p2.core.consumers.
"""
