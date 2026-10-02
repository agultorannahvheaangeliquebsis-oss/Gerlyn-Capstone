from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import Product, WarehouseStock
from sales.models import SalesTransaction

from .models import Branch


class BranchDeletionSafetyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='branch-owner', password='pw', role=User.Role.OWNER,
        )
        self.client.force_login(self.owner)

    def _branch(self, name):
        return Branch.objects.create(
            owner=self.owner, branch_name=name, location='Baliuag',
        )

    def test_web_refuses_to_delete_warehouse_stock(self):
        branch = self._branch('Stock Branch')
        product = Product.objects.create(
            sku_code='BR-DEL-1', product_name='Stored Product', unit_price=10,
        )
        WarehouseStock.objects.create(
            warehouse=branch.warehouse, product=product, quantity_on_hand=5,
        )

        response = self.client.post(reverse('branch_delete', args=[branch.pk]))

        self.assertRedirects(response, reverse('branch_list'))
        self.assertTrue(Branch.objects.filter(pk=branch.pk).exists())
        self.assertIn(
            'warehouse stock records',
            ' '.join(str(message) for message in get_messages(response.wsgi_request)),
        )

    def test_api_uses_the_same_blockers_and_preserves_business_history(self):
        branch = self._branch('History Branch')
        SalesTransaction.objects.create(branch=branch, recorded_by=self.owner)

        response = self.client.delete(
            reverse('api-branch-detail', args=[branch.pk]),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('sales history', response.json()['blockers'])
        self.assertTrue(Branch.objects.filter(pk=branch.pk).exists())
        self.assertEqual(SalesTransaction.objects.filter(branch=branch).count(), 1)

    def test_assigned_manager_blocks_deletion(self):
        branch = self._branch('Managed Branch')
        manager = User.objects.create_user(
            username='assigned-manager', password='pw',
            role=User.Role.BRANCH_MANAGER,
        )
        manager.manager_profile.branch = branch
        manager.manager_profile.save(update_fields=['branch'])

        response = self.client.delete(
            reverse('api-branch-detail', args=[branch.pk]),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('assigned managers', response.json()['blockers'])
        self.assertTrue(Branch.objects.filter(pk=branch.pk).exists())

    def test_empty_branch_can_be_deleted_by_web_and_api(self):
        web_branch = self._branch('Empty Web Branch')
        api_branch = self._branch('Empty API Branch')

        web_response = self.client.post(
            reverse('branch_delete', args=[web_branch.pk]),
        )
        api_response = self.client.delete(
            reverse('api-branch-detail', args=[api_branch.pk]),
        )

        self.assertRedirects(web_response, reverse('branch_list'))
        self.assertEqual(api_response.status_code, 204)
        self.assertFalse(Branch.objects.filter(pk__in=[web_branch.pk, api_branch.pk]).exists())
