"""p2 public access component tests"""
from django.test import TestCase

from p2.core.models import Volume
from p2.core.tests.utils import get_test_storage


class TestPublicAccess(TestCase):
    """Public access is now controlled via Volume.public_read."""

    def setUp(self):
        self.storage = get_test_storage()
        self.volume = Volume.objects.create(
            name='p2-unittest-public-access',
            storage=self.storage)

    def test_public_read_flag(self):
        """Volume.public_read=True grants anonymous read access."""
        self.assertFalse(self.volume.public_read)
        self.volume.public_read = True
        self.volume.save()
        self.volume.refresh_from_db()
        self.assertTrue(self.volume.public_read)
