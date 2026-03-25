"""S3 Auth tests"""
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.test import LiveServerTestCase

from p2.api.models import APIKey


class AuthenticationTests(LiveServerTestCase):
    """Test Authentication"""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='p2_unittest',
            email='test@test.test',
            password=uuid4().hex)
        self.access_key, _ = APIKey.objects.get_or_create(
            user=self.user)
        self.session = boto3.session.Session()

    def test_unknown_access_key(self):
        """Test with an unknown access key"""
        boto3 = self.session.client(
            service_name='s3',
            aws_access_key_id="invalid",
            aws_secret_access_key="invalid",
            endpoint_url=self.live_server_url)
        with self.assertRaises(ClientError):
            boto3.list_buckets()

    def test_querystring(self):
        """Test a request with a querystring"""
        boto3 = self.session.client(
            service_name='s3',
            aws_access_key_id=self.access_key.access_key,
            aws_secret_access_key=self.access_key.decrypt_secret_key(),
            endpoint_url=self.live_server_url)
        versions = boto3.get_bucket_versioning(Bucket='test')
        self.assertEqual(versions['Status'], 'Disabled')
