"""p2 core http responses"""
import asyncio

from django.http import StreamingHttpResponse

from p2.core.constants import ATTR_BLOB_MIME, ATTR_BLOB_SIZE_BYTES
from p2.core.models import Blob


async def _blob_chunks(blob, chunk_size):
    """Async generator yielding blob content in chunks, running sync reads in a thread."""
    def _read():
        return blob.read(chunk_size)

    while True:
        chunk = await asyncio.to_thread(_read)
        if not chunk:
            break
        yield chunk


class BlobResponse(StreamingHttpResponse):
    """Directly return blob's content. Optionally return as attachment if as_download is True"""

    def __init__(self, blob: Blob, chunk_size=8192, as_download=True):
        super().__init__(_blob_chunks(blob, chunk_size))
        self['Content-Length'] = blob.attributes.get(ATTR_BLOB_SIZE_BYTES, 0)
        self['Content-Type'] = blob.attributes.get(ATTR_BLOB_MIME, 'text/plain')
        if as_download:
            self['Content-Disposition'] = f'attachment; filename="{blob.filename}"'
        else:
            self['Content-Disposition'] = 'inline'
