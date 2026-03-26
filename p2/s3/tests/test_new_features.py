"""Integration tests for new S3 features (require PostgreSQL + live server)."""
import json

from botocore.exceptions import ClientError

from p2.core.acl import VolumeACL
from p2.core.models import Blob
from p2.s3.tests.utils import S3TestCase


class BucketPolicyTests(S3TestCase):
    """Test GetBucketPolicy / PutBucketPolicy / DeleteBucketPolicy."""

    def setUp(self):
        super().setUp()
        VolumeACL.objects.create(
            volume=self.volume, user=self.user,
            permissions=['read', 'write', 'delete', 'list', 'admin'],
        )

    def _put_policy(self, policy_dict):
        return self.boto3.put_bucket_policy(
            Bucket='test-1',
            Policy=json.dumps(policy_dict),
        )

    def test_put_get_delete_policy(self):
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::test-1/*",
            }],
        }
        self._put_policy(policy)
        resp = self.boto3.get_bucket_policy(Bucket='test-1')
        got = json.loads(resp['Policy'])
        self.assertEqual(got['Statement'][0]['Action'], 's3:GetObject')

        self.boto3.delete_bucket_policy(Bucket='test-1')
        with self.assertRaises(ClientError) as ctx:
            self.boto3.get_bucket_policy(Bucket='test-1')
        self.assertIn('NoSuchBucketPolicy', str(ctx.exception))

    def test_put_invalid_policy(self):
        with self.assertRaises(ClientError):
            self.boto3.put_bucket_policy(
                Bucket='test-1', Policy='not json')

    def test_get_no_policy(self):
        with self.assertRaises(ClientError):
            self.boto3.get_bucket_policy(Bucket='test-1')


class UploadPartCopyTests(S3TestCase):
    """Test UploadPartCopy — copy source object as a multipart part."""

    def setUp(self):
        super().setUp()
        VolumeACL.objects.create(
            volume=self.volume, user=self.user,
            permissions=['read', 'write', 'delete', 'list', 'admin'],
        )

    def test_upload_part_copy(self):
        # Put a source object
        src_data = b'A' * 1024
        self.boto3.put_object(Bucket='test-1', Key='source.bin', Body=src_data)

        # Start multipart upload
        mp = self.boto3.create_multipart_upload(Bucket='test-1', Key='dest.bin')
        upload_id = mp['UploadId']

        # Copy source as part 1
        copy_resp = self.boto3.upload_part_copy(
            Bucket='test-1', Key='dest.bin',
            UploadId=upload_id, PartNumber=1,
            CopySource={'Bucket': 'test-1', 'Key': 'source.bin'},
        )
        self.assertIn('CopyPartResult', copy_resp)
