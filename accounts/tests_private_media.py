"""Direct-link regression tests for personal uploads and module denial."""

import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from branches.models import Branch


class PrivateAvatarAccessTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix='gerlyn-private-avatar-')
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        storage_patch = patch.object(
            User._meta.get_field('avatar'), 'storage',
            FileSystemStorage(location=self.media_root, base_url=None),
        )
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.user = User.objects.create_user(
            username='avatar-owner', password='test-password', role=User.Role.CUSTOMER,
        )
        self.other = User.objects.create_user(
            username='avatar-other', password='test-password', role=User.Role.CUSTOMER,
        )
        self.user.avatar.save('avatar.png', ContentFile(b'private-avatar-bytes'), save=True)

    def test_raw_media_path_cannot_be_opened_by_any_account(self):
        path = f'/media/{self.user.avatar.name}'
        self.assertEqual(Client().get(path).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_avatar_endpoint_requires_current_account_session(self):
        url = reverse('accounts:avatar')
        self.assertEqual(Client().get(url).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'private-avatar-bytes')
        self.assertIn('no-store', response['Cache-Control'])
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)


class PublicBranchDataTests(TestCase):
    def test_storefront_api_does_not_disclose_owner_identity(self):
        owner = User.objects.create_user(
            username='branch-owner-private', password='test-password',
            role=User.Role.OWNER,
        )
        branch = Branch.objects.create(
            branch_name='Public Shop', location='Baliuag', owner=owner,
        )
        response = self.client.get(f'/api/branches/{branch.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'Public Shop')
        self.assertNotIn('ownerId', response.data)
        self.assertNotIn('ownerName', response.data)
        self.client.force_login(owner)
        self.assertIn('ownerId', self.client.get(f'/api/branches/{branch.pk}/').data)


class DirectPortalAccessTests(TestCase):
    def test_supplier_portal_direct_link_requires_supplier_role(self):
        url = reverse('supplier_portal')
        self.assertEqual(self.client.get(url).status_code, 302)
        customer = User.objects.create_user(
            username='portal-customer', password='pw', role=User.Role.CUSTOMER,
        )
        self.client.force_login(customer)
        self.assertEqual(self.client.get(url).status_code, 403)
