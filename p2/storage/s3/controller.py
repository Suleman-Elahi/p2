"""p2 s3 store controller"""
import logging
from io import RawIOBase
from tempfile import SpooledTemporaryFile
from typing import AsyncIterator

import boto3
from botocore.exceptions import ClientError

from p2.core.constants import ATTR_BLOB_MIME, ATTR_BLOB_SIZE_BYTES
from p2.core.storages.base import AsyncStorageController, StorageController, async_retry
from p2.storage.s3.constants import (TAG_ACCESS_KEY, TAG_ENDPOINT,
                                     TAG_ENDPOINT_SSL_VERIFY, TAG_REGION,
                                     TAG_SECRET_KEY)

LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024  # 64 KB
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class S3StorageController(StorageController):
    """S3 storage controller, save blobs as files"""

    def __init__(self, instance):
        super().__init__(instance)
        session = boto3.session.Session()
        self._client = session.client(
            service_name='s3',
            aws_access_key_id=self.instance.tags.get(TAG_ACCESS_KEY),
            aws_secret_access_key=self.instance.tags.get(TAG_SECRET_KEY),
            endpoint_url=self.instance.tags.get(TAG_ENDPOINT, None),
            region_name=self.instance.tags.get(TAG_REGION),
            verify=self.instance.tags.get(TAG_ENDPOINT_SSL_VERIFY, True))

    def get_required_tags(self):
        return [
            TAG_ACCESS_KEY,
            TAG_SECRET_KEY,
            TAG_REGION,
        ]

    def collect_attributes(self, blob):
        """Collect attributes such as size and mime type"""

    def _ensure_bucket_exists(self, name):
        """Ensure bucket exists before we attempt any object operations"""
        try:
            self._client.create_bucket(
                Bucket=name,
                CreateBucketConfiguration={
                    'LocationConstraint': self.instance.tags.get(TAG_REGION)
                },
            )
        except ClientError:
            pass

    def get_read_handle(self, blob) -> RawIOBase:
        _handle = SpooledTemporaryFile()
        self._ensure_bucket_exists(blob.volume.name)
        self._client.download_fileobj(blob.volume.name, blob.path, _handle)
        return _handle

    def commit(self, blob, handle: RawIOBase):
        self._ensure_bucket_exists(blob.volume.name)
        self._client.upload_fileobj(handle, blob.volume.name, blob.path)

    def delete(self, blob):
        self._ensure_bucket_exists(blob.volume.name)
        self._client.delete_object(
            Bucket=blob.volume.name,
            Key=blob.path[1:])


def _is_retryable_client_error(exc: ClientError) -> bool:
    """Return True if the ClientError has a retryable HTTP status code."""
    status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
    return status in _RETRYABLE_STATUS_CODES


class AsyncS3StorageController(AsyncStorageController):
    """Async S3 storage controller using aiobotocore for non-blocking S3 I/O."""

    def __init__(self, tags: dict):
        self.tags = tags

    def _client_kwargs(self) -> dict:
        """Build keyword arguments for the aiobotocore S3 client."""
        return {
            'service_name': 's3',
            'aws_access_key_id': self.tags.get(TAG_ACCESS_KEY),
            'aws_secret_access_key': self.tags.get(TAG_SECRET_KEY),
            'endpoint_url': self.tags.get(TAG_ENDPOINT, None),
            'region_name': self.tags.get(TAG_REGION),
            'verify': self.tags.get(TAG_ENDPOINT_SSL_VERIFY, True),
        }

    async def _get_read_stream(self, blob) -> AsyncIterator[bytes]:
        """Yield 64 KB chunks from S3 object body asynchronously.

        Note: retry is not applied here because async generators cannot be
        transparently retried by a decorator — callers should handle transient
        errors at the consumer level if needed.
        """
        import aiobotocore.session  # local import to avoid hard dep at module load
        session = aiobotocore.session.get_session()
        async with session.create_client(**self._client_kwargs()) as client:
            response = await client.get_object(
                Bucket=blob.volume.name,
                Key=blob.path,
            )
            body = response['Body']
            async for chunk in body.iter_chunks(CHUNK_SIZE):
                yield chunk

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(IOError, ClientError))
    async def _commit(self, blob, stream: AsyncIterator[bytes]) -> None:
        """Collect all chunks from the async iterator and upload to S3 via put_object."""
        import aiobotocore.session
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        data = b''.join(chunks)

        session = aiobotocore.session.get_session()
        async with session.create_client(**self._client_kwargs()) as client:
            await client.put_object(
                Bucket=blob.volume.name,
                Key=blob.path,
                Body=data,
            )
        LOGGER.debug('AsyncS3StorageController::Commit', extra={'blob': str(blob)})

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(IOError, ClientError))
    async def _delete(self, blob) -> None:
        """Delete the S3 object asynchronously."""
        import aiobotocore.session
        session = aiobotocore.session.get_session()
        async with session.create_client(**self._client_kwargs()) as client:
            await client.delete_object(
                Bucket=blob.volume.name,
                Key=blob.path,
            )
        LOGGER.debug('AsyncS3StorageController::Delete', extra={'blob': str(blob)})

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(IOError, ClientError))
    async def _collect_attributes(self, blob) -> dict:
        """Call head_object to get size and content-type; update blob.attributes."""
        import aiobotocore.session
        session = aiobotocore.session.get_session()
        async with session.create_client(**self._client_kwargs()) as client:
            response = await client.head_object(
                Bucket=blob.volume.name,
                Key=blob.path,
            )
        size = response.get('ContentLength', 0)
        mime_type = response.get('ContentType', 'application/octet-stream')
        blob.attributes[ATTR_BLOB_SIZE_BYTES] = str(size)
        blob.attributes[ATTR_BLOB_MIME] = mime_type
        return {
            ATTR_BLOB_SIZE_BYTES: str(size),
            ATTR_BLOB_MIME: mime_type,
        }
