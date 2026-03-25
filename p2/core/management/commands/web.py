"""p2 Webserver management command"""

import cherrypy
from django.core.management.base import BaseCommand
import logging

from p2.lib.config import CONFIG
from p2.root.wsgi import application

LOGGER = logging.getLogger(__name__)

class Command(BaseCommand):
    """Run CherryPy webserver"""

    def handle(self, *args, **options):
        """p2 cherrypy server"""
        cherrypy.config.update({
            'server.socket_host': '0.0.0.0',
            'server.socket_port': 8000,
            'server.thread_pool': 20,
            'log.screen': False,
            'log.access_file': '',
            'log.error_file': '',
            'server.max_request_body_size': 0,
            'server.socket_timeout': 600,
        })
        cherrypy.tree.graft(application, '/')
        cherrypy.engine.start()
        for file in CONFIG.loaded_file:
            cherrypy.engine.autoreload.files.add(file)
            LOGGER.info("Added file to autoreload triggers", file=file)
        cherrypy.engine.block()
