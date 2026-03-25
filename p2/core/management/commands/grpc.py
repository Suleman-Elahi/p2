"""p2 gRPC management command (async)"""

import asyncio
import logging

import grpc
import grpc.aio
from django.core.management.base import BaseCommand

from p2.grpc.protos import serve_pb2, serve_pb2_grpc
from p2.serve.grpc import Serve

LOGGER = logging.getLogger(__name__)

_GRPC_PORT = '[::]:50051'


async def serve_forever():
    """Start the async gRPC server and run until interrupted.

    Uses grpc.aio.server() for non-blocking request handling.
    Validates: Requirements 11.1
    """
    server = grpc.aio.server()
    serve_pb2_grpc.add_ServeServicer_to_server(Serve(), server)
    server.add_insecure_port(_GRPC_PORT)
    await server.start()
    LOGGER.info('gRPC server started on port 50051')
    await server.wait_for_termination()


class Command(BaseCommand):
    """Run async gRPC Server"""

    help = 'Start the async gRPC serve layer'

    def handle(self, *args, **options):
        """Start async gRPC server."""
        asyncio.run(serve_forever())
