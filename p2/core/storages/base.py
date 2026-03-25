"""p2 storage base controller"""
import asyncio
import functools
from io import RawIOBase
from tempfile import SpooledTemporaryFile
from typing import AsyncIterator

from p2.core.controllers import Controller
from p2.core.models import Blob
from p2.core.telemetry import tracer


def async_retry(max_attempts=3, base_delay=1.0, exceptions=(IOError,)):
    """Decorator for async functions: retry with exponential backoff on transient errors."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(base_delay * (2 ** attempt))
            raise last_exc
        return wrapper
    return decorator


class AsyncStorageController:
    """Base async storage controller."""

    async def get_read_stream(self, blob) -> AsyncIterator[bytes]:
        """Yield chunks of blob data asynchronously. Satisfies Requirement 9.5."""
        with tracer.start_as_current_span(
            "storage.read",
            attributes={"storage.operation": "read", "blob.pk": str(blob.pk)},
        ):
            async for chunk in self._get_read_stream(blob):
                yield chunk

    async def _get_read_stream(self, blob) -> AsyncIterator[bytes]:
        """Override in subclasses to yield blob data chunks."""
        raise NotImplementedError

    async def commit(self, blob, stream: AsyncIterator[bytes]) -> None:
        """Write blob data from async stream. Satisfies Requirement 9.5."""
        with tracer.start_as_current_span(
            "storage.write",
            attributes={"storage.operation": "write", "blob.pk": str(blob.pk)},
        ):
            await self._commit(blob, stream)

    async def _commit(self, blob, stream: AsyncIterator[bytes]) -> None:
        """Override in subclasses to write blob data."""
        raise NotImplementedError

    async def delete(self, blob) -> None:
        """Delete blob data. Satisfies Requirement 9.5."""
        with tracer.start_as_current_span(
            "storage.delete",
            attributes={"storage.operation": "delete", "blob.pk": str(blob.pk)},
        ):
            await self._delete(blob)

    async def _delete(self, blob) -> None:
        """Override in subclasses to delete blob data."""
        raise NotImplementedError

    async def collect_attributes(self, blob) -> dict:
        """Collect size, MIME type, etc. Satisfies Requirement 9.5."""
        with tracer.start_as_current_span(
            "storage.collect_attributes",
            attributes={"storage.operation": "collect_attributes", "blob.pk": str(blob.pk)},
        ):
            return await self._collect_attributes(blob)

    async def _collect_attributes(self, blob) -> dict:
        """Override in subclasses to collect blob attributes."""
        raise NotImplementedError


class StorageController(Controller):
    """Base Storage Controller Class"""

    form_class = 'p2.core.forms.StorageForm'

    def collect_attributes(self, blob: Blob):
        """Collect stats like size and mime type. This is being called during Blob's save"""

    def get_read_handle(self, blob: Blob) -> RawIOBase:
        """Return file-like object which can be used to manipulate payload."""
        raise NotImplementedError

    # pylint: disable=unused-argument
    def get_write_handle(self, blob: Blob) -> RawIOBase:
        """Return file-like object to write data into. Default implementation opens a temporary
        file in w+b mode."""
        return SpooledTemporaryFile(max_size=500)

    def commit(self, blob: Blob, handle: RawIOBase):
        """Called when blob is saved and data can be flushed to disk/remote"""
        raise NotImplementedError

    def delete(self, blob: Blob):
        """Delete Blob"""
        raise NotImplementedError
