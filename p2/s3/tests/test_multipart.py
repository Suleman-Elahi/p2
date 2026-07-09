"""S3 Multipart tests"""
import glob
import os
from tempfile import NamedTemporaryFile

from boto3.s3.transfer import TransferConfig

from p2.core.models import Volume
from p2.s3.tests.utils import S3TestCase


# pylint: disable=too-few-public-methods
class MultipartTests(S3TestCase):
    """Test Multipart-related operations"""

    def test_multipart_upload(self):
        """Test multipart upload — verify all chunk data is merged on disk"""
        config = TransferConfig(
            multipart_threshold=1024 * 25,
            max_concurrency=1,
            multipart_chunksize=1024 * 25,
            use_threads=False,
        )
        expected_size = 1024 * 1024  # 1 MB
        with NamedTemporaryFile(delete=False) as file:
            file_name = file.name
            try:
                data = os.urandom(expected_size)
                file.write(data)
                file.close()

                self.boto3.upload_file(
                    file_name, 'test-1', 'test-file-1', Config=config
                )

                # Get the object back and assert size and content match
                res = self.boto3.get_object(Bucket='test-1', Key='test-file-1')
                retrieved_data = res['Body'].read()
                self.assertEqual(len(retrieved_data), expected_size)
                self.assertEqual(retrieved_data, data)
            finally:
                if os.path.exists(file_name):
                    os.unlink(file_name)
