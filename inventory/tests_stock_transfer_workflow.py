from importlib import import_module
import threading

from django.apps import apps as django_apps
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from branches.models import Branch
from core.module_permissions import WITHDRAWALS
from inventory.models import (
    Batch, Inventory, Product, StockMovement, StockTransfer, StockTransferEvent,
    StockWriteOff, Warehouse, WarehouseFloorTransfer, WarehouseStock,
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
        self.assertTrue(WarehouseFloorTransfer.objects.filter(pk=transfer.pk).exists())
        self.assertFalse(StockWriteOff.objects.filter(pk=transfer.pk).exists())

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
                movement_type=StockMovement.MovementType.TRANSFER_DISCREPANCY,
                quantity_change=-2,
            ).exists()
        )
        self.assertFalse(StockMovement.objects.filter(
            reference_id=transfer.pk,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
        ).exists())
        self.assertEqual(
            StockTransferEvent.objects.filter(transfer=transfer).last().to_status,
            StockTransfer.Status.DISCREPANCY,
        )

    def test_web_discrepancy_reconciles_the_reported_product_only(self):
        second_product = Product.objects.create(
            sku_code='SKU-Y', product_name='Second Product', unit_price='12.00',
        )
        WarehouseStock.objects.create(
            warehouse=self.warehouse_a, product=second_product, quantity_on_hand=0,
        )
        WarehouseStock.objects.create(
            warehouse=self.warehouse_b, product=second_product, quantity_on_hand=20,
        )
        self.client.force_login(self.staff_a)
        created = self.client.post(
            reverse('api-stock-transfer-list'),
            data={
                'sourceBranchId': self.branch_b.pk,
                'destinationBranchId': self.branch_a.pk,
                'purpose': StockTransfer.Purpose.BRANCH_RESTOCK,
                'items': [
                    {'productId': self.product.pk, 'quantityRequested': 5},
                    {'productId': second_product.pk, 'quantityRequested': 4},
                ],
            },
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 201, created.content)
        transfer = StockTransfer.objects.get(pk=created.json()['id'])
        items = list(transfer.items.order_by('pk'))
        self.client.force_login(self.manager_b)
        approved = self.client.patch(
            reverse('api-stock-transfer-approve', args=[transfer.pk]),
            data={
                'action': 'approve',
                'items': {str(items[0].pk): 5, str(items[1].pk): 4},
            },
            content_type='application/json',
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        released = self.client.patch(
            reverse('api-stock-transfer-release', args=[transfer.pk]),
            content_type='application/json',
        )
        self.assertEqual(released.status_code, 200, released.content)

        self.client.force_login(self.staff_a)
        response = self.client.post(
            reverse('transfer_flag_discrepancy', args=[transfer.pk]),
            data={
                'item_id': [items[0].pk, items[1].pk],
                'quantity_received': [5, 2],
                'flag_reason': 'Two units of the second product were missing',
            },
        )

        self.assertRedirects(response, reverse('transfer_detail', args=[transfer.pk]))
        self.assertEqual(
            Inventory.objects.get(branch=self.branch_a, product=self.product).quantity_on_hand,
            5,
        )
        self.assertEqual(
            Inventory.objects.get(branch=self.branch_a, product=second_product).quantity_on_hand,
            2,
        )
        movement = StockMovement.objects.get(
            reference_id=transfer.pk,
            movement_type=StockMovement.MovementType.TRANSFER_DISCREPANCY,
        )
        self.assertEqual(movement.inventory.product, second_product)
        self.assertEqual(movement.quantity_change, -2)

    def test_write_off_uses_its_own_domain_and_movement_type(self):
        inventory = Inventory.objects.create(
            branch=self.branch_a, product=self.product, quantity_on_hand=6,
        )
        Batch.objects.create(
            branch=self.branch_a, product=self.product,
            location=Batch.Location.FLOOR, quantity=6,
            received_date='2026-01-01',
        )
        self.client.force_login(self.owner)
        created = self.client.post(
            reverse('api-stock-transfer-list'),
            data={
                'sourceBranchId': self.branch_a.pk,
                'purpose': StockTransfer.Purpose.DAMAGED_SPOILED,
                'items': [{'productId': self.product.pk, 'quantityRequested': 2}],
            },
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 201, created.content)
        write_off_id = created.json()['id']
        self.assertTrue(StockWriteOff.objects.filter(pk=write_off_id).exists())
        self.assertFalse(WarehouseFloorTransfer.objects.filter(pk=write_off_id).exists())

        item = StockTransfer.objects.get(pk=write_off_id).items.get()
        approved = self.client.patch(
            reverse('api-stock-transfer-approve', args=[write_off_id]),
            data={'action': 'approve', 'items': {str(item.pk): 2}},
            content_type='application/json',
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        completed = self.client.patch(
            reverse('api-stock-transfer-release', args=[write_off_id]),
            content_type='application/json',
        )
        self.assertEqual(completed.status_code, 200, completed.content)

        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity_on_hand, 4)
        movement = StockMovement.objects.get(reference_id=write_off_id)
        self.assertEqual(movement.movement_type, StockMovement.MovementType.WRITE_OFF)
        self.assertIn('Write-off', movement.notes)
        self.assertFalse(StockMovement.objects.filter(
            reference_id=write_off_id,
            movement_type__in=[
                StockMovement.MovementType.ADJUSTMENT,
                StockMovement.MovementType.MANUAL_OUT,
            ],
        ).exists())

        receipt = self.client.patch(
            reverse('api-stock-transfer-acknowledge', args=[write_off_id]),
            data={'receivedQty': 2},
            content_type='application/json',
        )
        self.assertEqual(receipt.status_code, 400)
        self.assertIn('do not have a floor-receipt step', receipt.content.decode())


class StockWorkflowMovementBackfillTests(TestCase):
    def test_only_identifiable_legacy_transfer_and_writeoff_movements_are_reclassified(self):
        owner = User.objects.create_user(
            username='backfill-owner', password='pw', role=User.Role.OWNER,
        )
        branch = Branch.objects.create(
            owner=owner, branch_name='Backfill Branch', location='Main',
        )
        product = Product.objects.create(
            sku_code='SKU-BACKFILL', product_name='Backfill Product', unit_price=10,
        )
        inventory = Inventory.objects.create(
            branch=branch, product=product, quantity_on_hand=5,
        )
        transfer = StockTransfer.objects.create(
            warehouse=branch.warehouse, source_branch=branch,
            destination_branch=branch,
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
            requested_by=owner,
        )
        write_off = StockTransfer.objects.create(
            warehouse=branch.warehouse, source_branch=branch,
            destination_branch=None,
            purpose=StockTransfer.Purpose.DAMAGED_SPOILED,
            requested_by=owner,
        )
        discrepancy = StockMovement.objects.create(
            inventory=inventory,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            quantity_change=-1, quantity_before=5, quantity_after=4,
            reference_id=transfer.pk,
            notes=f'Discrepancy on {transfer.slip_number}: one missing',
            created_by=owner,
        )
        legacy_write_off = StockMovement.objects.create(
            inventory=inventory,
            movement_type=StockMovement.MovementType.MANUAL_OUT,
            quantity_change=-1, quantity_before=4, quantity_after=3,
            reference_id=write_off.pk,
            notes=f'Withdrawal Slip {write_off.slip_number} — Damaged/Spoiled',
            created_by=owner,
        )
        unrelated = StockMovement.objects.create(
            inventory=inventory,
            movement_type=StockMovement.MovementType.ADJUSTMENT,
            quantity_change=1, quantity_before=3, quantity_after=4,
            reference_id=transfer.pk,
            notes='Count correction — annual physical count',
            created_by=owner,
        )

        migration = import_module(
            'inventory.migrations.0030_separate_transfer_writeoff_domains'
        )
        migration.classify_existing_movements(django_apps, None)

        discrepancy.refresh_from_db()
        legacy_write_off.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(
            discrepancy.movement_type,
            StockMovement.MovementType.TRANSFER_DISCREPANCY,
        )
        self.assertEqual(
            legacy_write_off.movement_type,
            StockMovement.MovementType.WRITE_OFF,
        )
        self.assertEqual(unrelated.movement_type, StockMovement.MovementType.ADJUSTMENT)


class ConcurrentStockTransferReleaseTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_user(
            username='concurrent-owner', password='pw', role=User.Role.OWNER,
        )
        self.manager = User.objects.create_user(
            username='concurrent-manager', password='pw',
            role=User.Role.BRANCH_MANAGER,
        )
        self.source = Branch.objects.create(
            owner=self.owner, branch_name='Concurrent Source', location='A',
        )
        self.destination = Branch.objects.create(
            owner=self.owner, branch_name='Concurrent Destination', location='B',
        )
        self.manager.manager_profile.branch = self.source
        self.manager.manager_profile.save(update_fields=['branch'])
        self.product = Product.objects.create(
            sku_code='CONCURRENT-SKU', product_name='Concurrent Product', unit_price=10,
        )
        self.stock = WarehouseStock.objects.create(
            warehouse=self.source.warehouse, product=self.product, quantity_on_hand=20,
        )
        self.transfer = WarehouseFloorTransfer.objects.create(
            warehouse=self.source.warehouse,
            source_branch=self.source,
            destination_branch=self.destination,
            requested_by=self.manager,
            approved_by=self.manager,
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
            status=StockTransfer.Status.APPROVED,
        )
        self.item = self.transfer.items.create(
            product=self.product, quantity_requested=5, quantity_approved=5,
        )

    def test_two_simultaneous_api_releases_apply_stock_once(self):
        barrier = threading.Barrier(3)
        statuses = []
        failures = []
        result_lock = threading.Lock()

        def release():
            close_old_connections()
            client = APIClient()
            client.force_authenticate(self.manager)
            try:
                barrier.wait(timeout=10)
                response = client.patch(
                    reverse('api-stock-transfer-release', args=[self.transfer.pk]),
                    data={}, format='json',
                )
                with result_lock:
                    statuses.append(response.status_code)
            except Exception as exc:  # surfaced by the assertion below
                with result_lock:
                    failures.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=release) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(failures, failures)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(statuses), [200, 400])
        self.stock.refresh_from_db()
        self.transfer.refresh_from_db()
        self.assertEqual(self.stock.quantity_on_hand, 15)
        self.assertEqual(self.transfer.status, StockTransfer.Status.RELEASED)
        self.assertEqual(
            Inventory.objects.get(
                branch=self.destination, product=self.product,
            ).quantity_on_hand,
            5,
        )
        self.assertEqual(
            StockMovement.objects.filter(reference_id=self.transfer.pk).count(),
            2,
        )
