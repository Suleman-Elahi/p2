"""S3 Bucket tests"""
from uuid import uuid4

import boto3
from django.contrib.auth.models import User
from django.test import LiveServerTestCase

from p2.api.models import APIKey
from p2.core.acl import VolumeACL
from p2.core.models import Storage, Volume
from p2.s3.constants import TAG_S3_DEFAULT_STORAGE


class BucketTests(LiveServerTestCase):
    """Test Bucket-related operations"""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='p2_unittest',
            email='test@test.test',
            password=uuid4().hex)
        self.access_key, _ = APIKey.objects.get_or_create(
            user=self.user)
        self.storage = Storage.objects.create(
            name='p2_s3_unittest',
            tags={
                TAG_S3_DEFAULT_STORAGE: True
            })
        session = boto3.session.Session()
        self.boto3 = session.client(
            service_name='s3',
            aws_access_key_id=self.access_key.access_key,
            aws_secret_access_key=self.access_key.decrypt_secret_key(),
            endpoint_url=self.live_server_url,
        )

    def test_list_buckets(self):
        """Test bucket list operation"""
        self.assertEqual(len(self.boto3.list_buckets()['Buckets']), 0)
        volume = Volume.objects.create(name='test-1', storage=self.storage)
        VolumeACL.objects.create(
            volume=volume,
            user=self.user,
            permissions=['read', 'write', 'list'],
        )
        self.assertEqual(len(self.boto3.list_buckets()['Buckets']), 1)
        self.assertEqual(self.boto3.list_buckets()['Buckets'][0]['Name'], 'test-1')
        volume.delete()
