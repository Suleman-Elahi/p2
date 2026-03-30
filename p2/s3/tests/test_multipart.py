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
        volume = Volume.objects.get(name='test-1')
        storage_dir = f"/storage/volumes/{volume.uuid.hex}"

        before_files = set(glob.glob(f"{storage_dir}/**/*", recursive=True))

        with NamedTemporaryFile() as file:
            file.write(os.urandom(expected_size))
            file.seek(0)
            self.boto3.upload_file(
                file.name, 'test-1', 'test-file-1', Config=config
            )

        after_files = set(glob.glob(f"{storage_dir}/**/*", recursive=True))
        new_files = [f for f in after_files - before_files if os.path.isfile(f)]

        # The merged result file must exist
        self.assertTrue(len(new_files) >= 1, f"Expected at least 1 new file, got: {new_files}")

        # Find the biggest new file — that is the merged output
        merged_file = max(new_files, key=os.path.getsize)
        self.assertEqual(os.path.getsize(merged_file), expected_size)
