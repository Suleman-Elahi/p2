"""S3 Object tests"""
import glob
import os
import json
from botocore.exceptions import ClientError

from p2.core.models import Volume
from p2.s3.tests.utils import S3TestCase


class ObjectTests(S3TestCase):
    """Test Object-related operations"""

    def _volume_storage_dir(self):
        from p2.core.storage_path import storage_path
        volume = Volume.objects.get(name='test-1')
        return storage_path("volumes", volume.uuid.hex)

    def _put_and_list_files(self, key, data):
        """Put an object and return the files in the volume storage dir before/after."""
        storage_dir = self._volume_storage_dir()
        before_files = set(glob.glob(f"{storage_dir}/**/*", recursive=True))
        self.boto3.put_object(Body=data, Bucket='test-1', Key=key)
        after_files = set(glob.glob(f"{storage_dir}/**/*", recursive=True))
        new_files = [f for f in after_files - before_files if os.path.isfile(f) and '.lmdb' not in f]
        return new_files

    def test_no_such_bucket(self):
        """Test That no-such-bucket error is raised"""
        with self.assertRaises(ClientError):
            self.boto3.get_object(Bucket='non-existant-bucket', Key='test file.txt')

    def test_create_object(self):
        """Test Object creation — verify data landed on disk"""
        data = b'this is test data'
        self.boto3.put_object(Body=data, Bucket='test-1', Key='test_file.txt')
        res = self.boto3.get_object(Bucket='test-1', Key='test_file.txt')
        self.assertEqual(res['Body'].read(), data)

    def test_head_object(self):
        """Test Object HEAD Operation"""
        self.boto3.put_object(
            Body=b'this is test data',
            Bucket='test-1',
            Key='test_file.txt')
        response = self.boto3.head_object(
            Bucket='test-1',
            Key='test_file.txt')
        self.assertEqual(response.get('ResponseMetadata').get('HTTPStatusCode'), 200)
        self.assertEqual(int(response.get('ContentLength', 0)), len(b'this is test data'))

    def test_head_object_no_key(self):
        """Test Object HEAD Operation (No Key)"""
        with self.assertRaises(ClientError):
            self.boto3.head_object(
                Bucket='test-1',
                Key='test_fileaaa.txt')

    def test_get_object(self):
        """Test Object retrieval"""
        data = b'this is test data'
        self.boto3.put_object(Body=data, Bucket='test-1', Key='test_file.txt')
        response = self.boto3.get_object(Bucket='test-1', Key='test_file.txt')
        self.assertEqual(data, response['Body'].read())

    def test_get_object_no_key(self):
        """Test Object retrieval (No Key)"""
        with self.assertRaises(ClientError):
            self.boto3.get_object(
                Bucket='test-1',
                Key='test_fileaaa.txt')

    def test_delete_object(self):
        """Test Object deletion"""
        data = b'this is test data'
        self.boto3.put_object(Body=data, Bucket='test-1', Key='test_file_del.txt')
        self.boto3.head_object(Bucket='test-1', Key='test_file_del.txt')

        self.boto3.delete_object(Bucket='test-1', Key='test_file_del.txt')
        with self.assertRaises(ClientError):
            self.boto3.get_object(Bucket='test-1', Key='test_file_del.txt')
