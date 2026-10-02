from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.module_permissions import WITHDRAWALS
from inventory.models import (
    Inventory, Product, StockMovement, StockTransfer, StockTransferEvent,
    Warehouse, WarehouseStock,
)


class CrossBranchStockRequestTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='test-pass', role=User.Role.OWNER,
        )
        self.branch_a = Branch.objects.create(
            owner=self.owner, branch_name='Branch A', location='A',
        )
        self.branch_b = Branch.objects.create(
            owner=self.owner, branch_name='Branch B', location='B',
        )
        self.branch_c = Branch.objects.create(
            owner=self.owner, branch_name='Branch C', location='C',
        )
        self.warehouse_a = self.branch_a.warehouse
        self.warehouse_b = self.branch_b.warehouse
        self.warehouse_c = self.branch_c.warehouse
        self.product = Product.objects.create(
            sku_code='SKU-X', product_name='Test Product', unit_price='10.00',
        )
        self.stock_a = WarehouseStock.objects.create(
            warehouse=self.warehouse_a, product=self.product, quantity_on_hand=0,
        )
        self.stock_b = WarehouseStock.objects.create(
            warehouse=self.warehouse_b, product=self.product, quantity_on_hand=20,
        )
        WarehouseStock.objects.create(
            warehouse=self.warehouse_c, product=self.product, quantity_on_hand=20,
        )
        self.manager_a = self._manager('manager-a', self.branch_a)
        self.manager_b = self._manager('manager-b', self.branch_b)
        self.manager_c = self._manager('manager-c', self.branch_c)
        self.staff_a = self._staff('staff-a', self.branch_a)

    def _manager(self, username, branch):
        user = User.objects.create_user(
            username=username, password='test-pass', role=User.Role.BRANCH_MANAGER,
        )
        user.manager_profile.branch = branch
        user.manager_profile.save(update_fields=['branch'])
        return user

    def _staff(self, username, branch):
        user = User.objects.create_user(
            username=username, password='test-pass', role=User.Role.STAFF,
        )
        user.staff_profile.branch = branch
        user.staff_profile.save(update_fields=['branch'])
        user.allowed_modules = list(set(user.allowed_modules + [WITHDRAWALS]))
        actions = dict(user.module_actions)
        actions[WITHDRAWALS] = ['add', 'edit']
        user.module_actions = actions
        user.save(update_fields=['allowed_modules', 'module_actions'])
        return user

    def _request_from_b(self, user=None, quantity=5):
        self.client.force_login(user or self.staff_a)
        return self.client.post(
            reverse('api-stock-transfer-list'),
            data={
                'sourceBranchId': self.branch_b.pk,
                'destinationBranchId': self.branch_a.pk,
                'purpose': StockTransfer.Purpose.BRANCH_RESTOCK,
                'items': [{'productId': self.product.pk, 'quantityRequested': quantity}],
            },
            content_type='application/json',
        )

    def test_destination_can_request_remote_stock_without_mutating_source(self):
        response = self._request_from_b()
        self.assertEqual(response.status_code, 201, response.content)
        transfer = StockTransfer.objects.get(pk=response.json()['id'])
        self.assertEqual(transfer.status, StockTransfer.Status.PENDING)
        self.assertEqual(transfer.source_branch, self.branch_b)
        self.assertEqual(transfer.destination_branch, self.branch_a)
        self.stock_b.refresh_from_db()
        self.assertEqual(self.stock_b.quantity_on_hand, 20)
        self.assertEqual(
            list(transfer.events.values_list('to_status', flat=True)),
            [StockTransfer.Status.PENDING],
        )

    def test_requester_cannot_redirect_stock_to_another_destination(self):
        self.client.force_login(self.staff_a)
        response = self.client.post(
            reverse('api-stock-transfer-list'),
            data={
                'sourceBranchId': self.branch_b.pk,
                'destinationBranchId': self.branch_c.pk,
                'purpose': StockTransfer.Purpose.BRANCH_RESTOCK,
                'items': [{'productId': self.product.pk, 'quantityRequested': 2}],
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(StockTransfer.objects.exists())

    def test_remote_request_is_blocked_until_local_warehouse_is_empty(self):
        self.stock_a.quantity_on_hand = 1
        self.stock_a.save()
        response = self._request_from_b()
        self.assertEqual(response.status_code, 400)
        self.assertIn('warehouse first', response.content.decode().lower())

    def test_only_source_branch_can_approve_and_release(self):
        transfer_id = self._request_from_b().json()['id']
        item_id = StockTransfer.objects.get(pk=transfer_id).items.get().pk
        approve_url = reverse('api-stock-transfer-approve', args=[transfer_id])
        release_url = reverse('api-stock-transfer-release', args=[transfer_id])

        self.client.force_login(self.manager_a)
        denied = self.client.patch(
            approve_url,
            data={'action': 'approve', 'items': {str(item_id): 5}},
            content_type='application/json',
        )
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.manager_b)
        approved = self.client.patch(
            approve_url,
            data={'action': 'approve', 'items': {str(item_id): 5}},
            content_type='application/json',
        )
        self.assertEqual(approved.status_code, 200, approved.content)

        self.client.force_login(self.manager_a)
        denied_release = self.client.patch(release_url, content_type='application/json')
        self.assertEqual(denied_release.status_code, 403)

        self.client.force_login(self.manager_b)
        released = self.client.patch(release_url, content_type='application/json')
        self.assertEqual(released.status_code, 200, released.content)
        self.stock_b.refresh_from_db()
        self.assertEqual(self.stock_b.quantity_on_hand, 15)
        self.assertEqual(
            Inventory.objects.get(branch=self.branch_a, product=self.product).quantity_on_hand,
            5,
        )

        duplicate = self.client.patch(release_url, content_type='application/json')
        self.assertEqual(duplicate.status_code, 400)
        self.stock_b.refresh_from_db()
        self.assertEqual(self.stock_b.quantity_on_hand, 15)

        transfer = StockTransfer.objects.get(pk=transfer_id)
        self.assertEqual(
            list(transfer.events.values_list('to_status', flat=True)),
            [
                StockTransfer.Status.PENDING,
                StockTransfer.Status.APPROVED,
                StockTransfer.Status.RELEASED,
            ],
        )

    def test_transfer_visibility_includes_both_parties_but_not_third_branch(self):
        transfer_id = self._request_from_b().json()['id']
        for user in (self.manager_a, self.manager_b):
            self.client.force_login(user)
            response = self.client.get(reverse('api-stock-transfer-list'))
            self.assertEqual([row['id'] for row in response.json()], [transfer_id])
        self.client.force_login(self.manager_c)
        response = self.client.get(reverse('api-stock-transfer-list'))
        self.assertEqual(response.json(), [])

    def test_discrepancy_reconciles_each_product_and_is_audited(self):
        transfer_id = self._request_from_b().json()['id']
        transfer = StockTransfer.objects.get(pk=transfer_id)
        item = transfer.items.get()
        self.client.force_login(self.manager_b)
        self.client.patch(
            reverse('api-stock-transfer-approve', args=[transfer_id]),
            data={'action': 'approve', 'items': {str(item.pk): 5}},
            content_type='application/json',
        )
        self.client.patch(
            reverse('api-stock-transfer-release', args=[transfer_id]),
            content_type='application/json',
        )
        self.client.force_login(self.staff_a)
        response = self.client.patch(
            reverse('api-stock-transfer-acknowledge', args=[transfer_id]),
            data={
                'receivedQty': 3,
                'flagReason': 'Two units missing on arrival',
                'items': {str(item.pk): 3},
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, StockTransfer.Status.DISCREPANCY)
        self.assertEqual(
            Inventory.objects.get(branch=self.branch_a, product=self.product).quantity_on_hand,
            3,
        )
        self.assertTrue(
            StockMovement.objects.filter(
                reference_id=transfer.pk,
                movement_type=StockMovement.MovementType.ADJUSTMENT,
                quantity_change=-2,
            ).exists()
        )
        self.assertEqual(
            StockTransferEvent.objects.filter(transfer=transfer).last().to_status,
            StockTransfer.Status.DISCREPANCY,
        )
