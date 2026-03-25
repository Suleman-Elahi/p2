"""S3 Multipart tests"""
import os
from hashlib import sha256
from tempfile import NamedTemporaryFile

from boto3.s3.transfer import TransferConfig

from p2.core.constants import ATTR_BLOB_HASH_SHA256
from p2.core.models import Blob
from p2.lib.hash import chunked_hasher
from p2.s3.tests.utils import S3TestCase


# pylint: disable=too-few-public-methods
class MultipartTests(S3TestCase):
    """Test Multipart-related operations"""

    def test_multipart_upload(self):
        """Test bucket list operation"""
        config = TransferConfig(multipart_threshold=1024 * 25,
                                max_concurrency=1, multipart_chunksize=1024 * 25, use_threads=False)
        with NamedTemporaryFile() as file:
            # Create 100 MB test file
            file.write(os.urandom(1024 * 1024 * 100))
            file.seek(0)
            h_digest = chunked_hasher(sha256(), file)
            self.boto3.upload_file(file.name, 'test-1', 'test-file-1',
                                   Config=config)
        blob = Blob.objects.get(path='/test-file-1')
        self.assertEqual(blob.attributes.get(ATTR_BLOB_HASH_SHA256), h_digest)
