from django.test import TestCase
from django.urls import reverse

from branches.models import Branch
from accounts.models import StaffProfile, User
from inventory.models import Inventory, Product


class PublicProductInactiveBranchIsolationTests(TestCase):
    def setUp(self):
        self.active_branch = Branch.objects.create(
            branch_name='Active Store', location='Baliuag', is_active=True,
        )
        self.inactive_branch = Branch.objects.create(
            branch_name='Closed Store', location='Baliuag', is_active=False,
        )
        self.product = Product.objects.create(
            sku_code='PUBLIC-ISO-1', product_name='Public Product', unit_price=25,
        )
        Inventory.objects.create(
            branch=self.active_branch, product=self.product, quantity_on_hand=3,
        )
        Inventory.objects.create(
            branch=self.inactive_branch, product=self.product, quantity_on_hand=99,
        )

    def test_public_availability_excludes_inactive_branches(self):
        response = self.client.get(reverse('api-product-list'))

        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json() if item['id'] == self.product.pk)
        self.assertEqual(row['availability'], [self.active_branch.branch_name])
        self.assertNotIn(self.inactive_branch.branch_name, response.content.decode())

    def test_inactive_branch_id_cannot_reveal_exact_stock(self):
        response = self.client.get(
            reverse('api-product-list'), {'branchId': self.inactive_branch.pk},
        )
        detail = self.client.get(
            reverse('api-product-detail', args=[self.product.pk]),
            {'branchId': self.inactive_branch.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertEqual(detail.status_code, 404)
        self.assertNotIn('99', response.content.decode())

    def test_active_branch_id_does_not_disclose_exact_stock(self):
        response = self.client.get(
            reverse('api-product-list'), {'branchId': self.active_branch.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertIsNone(response.json()[0]['stockAtBranch'])

    def test_staff_only_gets_exact_stock_for_assigned_branch(self):
        staff = User.objects.create_user(
            username='product-api-staff', password='pw', role=User.Role.STAFF,
        )
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': 'Staff', 'branch': self.active_branch},
        )
        self.client.force_login(staff)
        own = self.client.get(reverse('api-product-list'),
                              {'branchId': self.active_branch.pk})
        self.assertEqual(own.json()[0]['stockAtBranch'], 3)
        second_branch = Branch.objects.create(
            branch_name='Second Active Store', location='Baliuag', is_active=True,
        )
        Inventory.objects.create(
            branch=second_branch, product=self.product, quantity_on_hand=7,
        )
        other = self.client.get(reverse('api-product-list'),
                                {'branchId': second_branch.pk})
        self.assertIsNone(other.json()[0]['stockAtBranch'])
