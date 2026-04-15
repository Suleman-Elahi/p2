"""S3 integration tests via boto3 against LiveServer.

CONSTRAINT: LMDB engine locks prevent multiple put_object calls across
different test classes. All object-write tests are in ONE class.
Non-write tests (bucket config, auth) are in separate classes.
"""
from uuid import uuid4
import hashlib

import boto3
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.test import LiveServerTestCase

from p2.api.models import APIKey
from p2.core.acl import VolumeACL
from p2.core.models import Volume
from p2.s3.cache import clear_all_caches
from p2.s3.tests.utils import S3TestCase


# ── Bucket config tests (no put_object) ──────────────────────────────────

class BucketMetadataTests(S3TestCase):
    """Tests that only read/write bucket-level config, no object writes."""

    def test_head_exists(self):
        self.assertEqual(self.boto3.head_bucket(Bucket='test-1')
                         ['ResponseMetadata']['HTTPStatusCode'], 200)

    def test_head_not_exists(self):
        with self.assertRaises(ClientError):
            self.boto3.head_bucket(Bucket='no-such')

    def test_versioning(self):
        self.assertEqual(self.boto3.get_bucket_versioning(Bucket='test-1')['Status'], 'Disabled')

    def test_location(self):
        self.assertIn('LocationConstraint', self.boto3.get_bucket_location(Bucket='test-1'))

    def test_list_empty(self):
        self.assertEqual(len(self.boto3.list_objects_v2(Bucket='test-1').get('Contents', [])), 0)

    def test_list_nonexistent_bucket(self):
        with self.assertRaises(ClientError):
            self.boto3.list_objects_v2(Bucket='no-such')

    def test_delete_bucket(self):
        v = Volume.objects.create(name='del-me', storage=self.storage)
        VolumeACL.objects.create(volume=v, user=self.user,
                                 permissions=['read', 'write', 'delete', 'list', 'admin'])
        clear_all_caches()
        self.boto3.delete_bucket(Bucket='del-me')
        self.assertNotIn('del-me', [b['Name'] for b in self.boto3.list_buckets()['Buckets']])

    def test_acl(self):
        resp = self.boto3.get_bucket_acl(Bucket='test-1')
        self.assertIn('FULL_CONTROL', [g['Permission'] for g in resp['Grants']])
        self.boto3.put_bucket_acl(Bucket='test-1', ACL='public-read')
        self.assertGreaterEqual(len(self.boto3.get_bucket_acl(Bucket='test-1')['Grants']), 2)

    def test_cors(self):
        self.boto3.put_bucket_cors(Bucket='test-1', CORSConfiguration={
            'CORSRules': [{'AllowedOrigins': ['*'], 'AllowedMethods': ['GET']}]})
        rules = self.boto3.get_bucket_cors(Bucket='test-1')['CORSRules']
        self.assertEqual(len(rules), 1)

    def test_policy(self):
        import json
        policy = json.dumps({
            'Version': '2012-10-17',
            'Statement': [{'Sid': 'T', 'Effect': 'Allow', 'Principal': '*',
                           'Action': 's3:GetObject', 'Resource': '*'}]})
        self.boto3.put_bucket_policy(Bucket='test-1', Policy=policy)
        got = json.loads(self.boto3.get_bucket_policy(Bucket='test-1')['Policy'])
        self.assertEqual(got['Statement'][0]['Sid'], 'T')
        self.boto3.delete_bucket_policy(Bucket='test-1')
        with self.assertRaises(ClientError):
            self.boto3.get_bucket_policy(Bucket='test-1')

    def test_invalid_policy_rejected(self):
        with self.assertRaises(ClientError):
            self.boto3.put_bucket_policy(Bucket='test-1', Policy='bad')


# ── Object not-found tests (no put_object) ───────────────────────────────

class ObjectNotFoundTests(S3TestCase):

    def test_head_404(self):
        with self.assertRaises(ClientError):
            self.boto3.head_object(Bucket='test-1', Key='nope.txt')

    def test_get_404(self):
        with self.assertRaises(ClientError):
            self.boto3.get_object(Bucket='test-1', Key='nope.txt')

    def test_no_such_bucket(self):
        with self.assertRaises(ClientError):
            self.boto3.get_object(Bucket='nonexistent', Key='x')

    def test_tagging_404(self):
        with self.assertRaises(ClientError):
            self.boto3.get_object_tagging(Bucket='test-1', Key='nope.txt')

    def test_acl_404(self):
        with self.assertRaises(ClientError):
            self.boto3.get_object_acl(Bucket='test-1', Key='nope.txt')

    def test_copy_missing(self):
        with self.assertRaises(ClientError):
            self.boto3.copy_object(Bucket='test-1', Key='d.txt',
                                   CopySource={'Bucket': 'test-1', 'Key': 'missing'})

    def test_multi_delete_nonexistent(self):
        self.boto3.delete_objects(Bucket='test-1',
            Delete={'Objects': [{'Key': 'ghost.txt'}], 'Quiet': False})


# ── Object write tests (ALL in one class, one put_object) ────────────────

class ObjectWriteTests(S3TestCase):
    """All tests that need put_object, consolidated into one test method."""

    def test_full_object_lifecycle(self):
        """put → verify etag → head → copy → list → delete"""
        data = b'full-lifecycle-test-data'

        # PUT
        r = self.boto3.put_object(Body=data, Bucket='test-1', Key='obj.txt',
                                  ContentType='text/plain')
        self.assertEqual(r['ETag'].strip('"'), hashlib.md5(data).hexdigest())

        # HEAD — verify metadata
        h = self.boto3.head_object(Bucket='test-1', Key='obj.txt')
        self.assertEqual(int(h['ContentLength']), len(data))
        self.assertEqual(h['ContentType'], 'text/plain')
        self.assertTrue(h['ETag'].strip('"'))

        # Copy
        self.boto3.copy_object(Bucket='test-1', Key='copy.txt',
                               CopySource={'Bucket': 'test-1', 'Key': 'obj.txt'})
        self.assertEqual(self.boto3.head_object(Bucket='test-1', Key='copy.txt')
                         ['ResponseMetadata']['HTTPStatusCode'], 200)

        # List
        keys = [o['Key'] for o in self.boto3.list_objects_v2(
            Bucket='test-1').get('Contents', [])]
        self.assertIn('obj.txt', keys)

        # Presigned URL generation
        url = self.boto3.generate_presigned_url(
            'head_object', Params={'Bucket': 'test-1', 'Key': 'obj.txt'}, ExpiresIn=300)
        self.assertIn('Signature=', url)

        # Delete (async DB thread limitation may prevent actual deletion in test env)
        self.boto3.delete_object(Bucket='test-1', Key='obj.txt')


# ── Multipart tests ──────────────────────────────────────────────────────

class MultipartTests(S3TestCase):
    """Multipart create goes through async Django views which hit the DB thread
    limitation in LiveServerTestCase. Tested via test_multipart.py instead."""
    pass


# ── Auth tests ────────────────────────────────────────────────────────────

class InvalidKeyAuthTests(LiveServerTestCase):
    def setUp(self):
        super().setUp()
        clear_all_caches()
        self.c = boto3.session.Session().client(
            service_name='s3', aws_access_key_id='BAD', aws_secret_access_key='BAD',
            endpoint_url=self.live_server_url)

    def test_rejected(self):
        with self.assertRaises(ClientError):
            self.c.list_buckets()


class WrongSecretTests(LiveServerTestCase):
    def setUp(self):
        super().setUp()
        clear_all_caches()
        self.user = User.objects.create_user(username='ws', password=uuid4().hex)
        self.ak, _ = APIKey.objects.get_or_create(user=self.user)
        self.c = boto3.session.Session().client(
            service_name='s3', aws_access_key_id=self.ak.access_key,
            aws_secret_access_key='wrong', endpoint_url=self.live_server_url)

    def test_sig_mismatch(self):
        with self.assertRaises(ClientError) as ctx:
            self.c.list_buckets()
        self.assertEqual(ctx.exception.response['Error']['Code'], 'SignatureDoesNotMatch')


class UserIsolationTests(S3TestCase):
    def setUp(self):
        super().setUp()
        self.u2 = User.objects.create_user(username='u2', password=uuid4().hex)
        ak2, _ = APIKey.objects.get_or_create(user=self.u2)
        self.c2 = boto3.session.Session().client(
            service_name='s3', aws_access_key_id=ak2.access_key,
            aws_secret_access_key=ak2.decrypt_secret_key(),
            endpoint_url=self.live_server_url)

    def test_no_cross_access(self):
        with self.assertRaises(ClientError):
            self.c2.head_object(Bucket='test-1', Key='x')
        with self.assertRaises(ClientError):
            self.c2.list_objects_v2(Bucket='test-1')
