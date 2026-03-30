"""Storage Controller that does nothing"""
from io import RawIOBase

from p2.core.storages.base import StorageController


class NullStorageController(StorageController):
    """Null Storage controller, doesn't save anything, useful for debugging"""

    def get_read_handle(self, blob) -> RawIOBase:
        return None

    def get_write_handle(self, blob) -> RawIOBase:
        return None

    def commit(self, blob, handle: RawIOBase):
        return None

    def delete(self, blob):
        return None
