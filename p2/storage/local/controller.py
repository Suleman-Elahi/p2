"""p2 local store controller"""
import mimetypes
import os
from io import RawIOBase
from shutil import copyfileobj
from typing import AsyncIterator

import aiofiles
import aiofiles.os
import magic
import logging

from p2.core.constants import (ATTR_BLOB_IS_TEXT, ATTR_BLOB_MIME,
                               ATTR_BLOB_SIZE_BYTES)
from p2.core.models import Blob
from p2.core.storages.base import AsyncStorageController, StorageController
from p2.storage.local.constants import TAG_ROOT_PATH

LOGGER = logging.getLogger(__name__)
TEXT_CHARACTERS = str.encode("".join(list(map(chr, range(32, 127))) + list("\n\r\t\b")))

class LocalStorageController(StorageController):
    """Local storage controller, save blobs as files"""

    def get_required_tags(self):
        return [
            TAG_ROOT_PATH
        ]

    def _build_subdir(self, blob: Blob) -> str:
        """get 1e/2f/ from blob where UUID starts with 1e2f"""
        return os.path.sep.join([
            blob.uuid.hex[0:2],
            blob.uuid.hex[2:4]
        ])

    def _build_path(self, blob: Blob) -> str:
        root = self.tags.get(TAG_ROOT_PATH)
        return os.path.join(root, self._build_subdir(blob), blob.uuid.hex)

    def is_text(self, filename):
        """Return True if file is text, else False"""
        payload = open(filename, 'rb').read(512)
        _null_trans = bytes.maketrans(b"", b"")
        if not payload:
            # Empty files are considered text
            return True
        if b"\0" in payload:
            # Files with null bytes are likely binary
            return False
        # Get the non-text characters (maps a character to itself then
        # use the 'remove' option to get rid of the text characters.)
        translation = payload.translate(_null_trans, TEXT_CHARACTERS)
        # If more than 30% non-text characters, then
        # this is considered a binary file
        if float(len(translation)) / float(len(payload)) > 0.30:
            return False
        return True

    def collect_attributes(self, blob: Blob):
        """Collect attributes such as size and mime type"""
        if os.path.exists(self._build_path(blob)):
            mime_type = magic.from_file(self._build_path(blob), mime=True)
            size = os.stat(self._build_path(blob)).st_size
            blob.attributes[ATTR_BLOB_MIME] = mime_type
            blob.attributes[ATTR_BLOB_IS_TEXT] = self.is_text(self._build_path(blob))
            blob.attributes[ATTR_BLOB_SIZE_BYTES] = str(size)
            LOGGER.debug('Updated size to Blob', size=size, blob=blob)

    def get_read_handle(self, blob: Blob) -> RawIOBase:
        fs_path = self._build_path(blob)
        LOGGER.debug('LocalStorageController::Retrieve', blob=blob, file=fs_path)
        if os.path.exists(fs_path) and os.path.isfile(fs_path):
            return open(fs_path, 'rb')
        LOGGER.warning("File does not exist or is not a file.", file=fs_path)
        return None

    def commit(self, blob: Blob, handle: RawIOBase):
        fs_path = self._build_path(blob)
        os.makedirs(os.path.dirname(fs_path), exist_ok=True)
        LOGGER.debug('LocalStorageController::Commit', blob=blob, file=fs_path)
        with open(fs_path, 'wb') as _dest:
            return copyfileobj(handle, _dest)

    def delete(self, blob: Blob):
        fs_path = self._build_path(blob)
        os.makedirs(os.path.dirname(fs_path), exist_ok=True)
        # Not file_like, delete file if it exists
        if os.path.exists(fs_path) and os.path.isfile(fs_path):
            os.unlink(fs_path)
            LOGGER.debug("LocalStorageController::Delete", file=fs_path)
        else:
            LOGGER.warning("File does not exist during deletion attempt.", file=fs_path)


CHUNK_SIZE = 64 * 1024  # 64 KB


class AsyncLocalStorageController(AsyncStorageController):
    """Async local storage controller using aiofiles for filesystem I/O."""

    def __init__(self, tags: dict):
        self.tags = tags

    def _build_path(self, blob: Blob) -> str:
        root = self.tags.get(TAG_ROOT_PATH)
        return os.path.join(root, blob.uuid.hex)

    async def _get_read_stream(self, blob: Blob) -> AsyncIterator[bytes]:
        """Yield 64 KB chunks from the blob file asynchronously."""
        fs_path = self._build_path(blob)
        async with aiofiles.open(fs_path, 'rb') as f:
            while True:
                chunk = await f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    async def _commit(self, blob: Blob, stream: AsyncIterator[bytes]) -> None:
        """Write blob data from an async iterator to the filesystem."""
        fs_path = self._build_path(blob)
        os.makedirs(os.path.dirname(fs_path), exist_ok=True)
        async with aiofiles.open(fs_path, 'wb') as f:
            async for chunk in stream:
                await f.write(chunk)

    async def _collect_attributes(self, blob: Blob) -> dict:
        """Collect size and MIME type using stdlib; update blob.attributes."""
        fs_path = self._build_path(blob)
        size = os.path.getsize(fs_path)
        mime_type, _ = mimetypes.guess_type(fs_path)
        if mime_type is None:
            mime_type = 'application/octet-stream'
        blob.attributes[ATTR_BLOB_SIZE_BYTES] = str(size)
        blob.attributes[ATTR_BLOB_MIME] = mime_type
        return {
            ATTR_BLOB_SIZE_BYTES: str(size),
            ATTR_BLOB_MIME: mime_type,
        }

    async def _delete(self, blob: Blob) -> None:
        """Delete the blob file asynchronously."""
        fs_path = self._build_path(blob)
        try:
            await aiofiles.os.remove(fs_path)
        except FileNotFoundError:
            LOGGER.warning("File does not exist during async deletion attempt.", file=fs_path)
