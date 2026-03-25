"""p2 Worker management command — runs the arq async worker."""
import logging

from django.core.management.base import BaseCommand

LOGGER = logging.getLogger(__name__)


class Command(BaseCommand):
    """Run arq Worker"""

    help = "Run the arq async task worker"

    def handle(self, *args, **options):
        """Start arq worker"""
        import asyncio
        from arq import run_worker
        from p2.core.worker import WorkerSettings
        asyncio.run(run_worker(WorkerSettings))
