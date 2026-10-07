"""Floor-refill requests: validate on submit, move stock only on release.

The Owner-submit test is a TransactionTestCase on purpose. A plain TestCase
wraps every test in one transaction, which hides the bug it guards against:
select_for_update() outside a transaction (what the view used to do for the
Owner's auto-approval) only raises in autocommit mode — i.e. in production.
"""
from datetime import date

from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import (
    Batch, Inventory, Product, StockMovement, StockTransfer, WarehouseStock,
)
from inventory.services import (
    StockTransferRequestError, create_warehouse_floor_transfer,
)


class _RefillFixture:
    """Savers-style branch: one warehouse, tuna (16) and sardines (10)."""

    def build(self):
        self.owner = User.objects.create_user(
            username='fr-owner', password='pw', role=User.Role.OWNER,
        )
        self.branch = Branch.objects.create(
            owner=self.owner, branch_name='Savers Mini Mart', location='S',
        )
        self.tuna = self._product('TUNA', 'Century Tuna Flakes in Oil 155g', 16)
        self.sardines = self._product('SARD', 'Sardines 155g', 10)
        self.manager = User.objects.create_user(
            username='fr-mgr', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        self.manager.manager_profile.branch = self.branch
        self.manager.manager_profile.save(update_fields=['branch'])

    def _product(self, sku, name, qty):
        product = Product.objects.create(sku_code=sku, product_name=name, unit_price='30.00')
        WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=product, quantity_on_hand=qty,
        )
        Batch.objects.create(
            product=product, branch=self.branch, location=Batch.Location.WAREHOUSE,
            quantity=qty, received_date=date.today(), expiry_date=date(2030, 1, 1),
        )
        return product

    def warehouse_qty(self, product):
        return WarehouseStock.objects.get(
            warehouse=self.branch.warehouse, product=product,
        ).quantity_on_hand

    def floor_qty(self, product):
        return Inventory.objects.filter(
            branch=self.branch, product=product,
        ).values_list('quantity_on_hand', flat=True).first() or 0

    def submit(self, rows, user=None):
        self.client.force_login(user or self.owner)
        return self.client.post(reverse('warehouse_stock_request'), {
            'branch_id': self.branch.pk,
            'source_branch_id': self.branch.pk,
            'product_id': [product.pk for product, _qty in rows],
            'quantity_requested': [qty for _product, qty in rows],
        })

    @staticmethod
    def flashed(response):
        return ' '.join(str(m) for m in response.context['messages'])


class OwnerSubmitInAutocommitTests(_RefillFixture, TransactionTestCase):
    def setUp(self):
        self.build()

    def test_owner_submit_does_not_500_and_moves_no_stock(self):
        response = self.submit([(self.tuna, 5)])
        self.assertEqual(response.status_code, 302)
        transfer = StockTransfer.objects.get()
        # Owner auto-approval grants the approval step only; nothing moves.
        self.assertEqual(transfer.status, StockTransfer.Status.APPROVED)
        self.assertEqual(self.warehouse_qty(self.tuna), 16)
        self.assertEqual(self.floor_qty(self.tuna), 0)
        self.assertFalse(StockMovement.objects.exists())

    def test_owner_over_available_is_a_validation_error_not_a_500(self):
        response = self.submit([(self.tuna, 50)])
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'Insufficient warehouse stock for Century Tuna Flakes in Oil 155g. '
            'Only 16 units are available.',
            self.flashed(response),
        )
        self.assertFalse(StockTransfer.objects.exists())


class RefillRequestValidationTests(_RefillFixture, TestCase):
    def setUp(self):
        self.build()

    def test_modal_caps_each_request_qty_at_its_available_quantity(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('warehouse_stock_list'),
            {'open_request': 1, 'branch': self.branch.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'max="16"')
        self.assertContains(response, 'data-available="16"')
        self.assertContains(response, 'max="10"')

    def test_zero_quantity_creates_nothing(self):
        response = self.submit([(self.tuna, 0)])
        self.assertEqual(response.status_code, 200)
        self.assertIn('at least one product', self.flashed(response))
        self.assertFalse(StockTransfer.objects.exists())

    def test_service_rejects_zero_and_negative_quantities(self):
        for qty in (0, -3):
            with self.assertRaises(StockTransferRequestError):
                create_warehouse_floor_transfer(
                    actor=self.manager, source_branch_id=self.branch.pk,
                    destination_branch_id=self.branch.pk,
                    items=[{'product_id': self.tuna.pk, 'quantity': qty}],
                )
        self.assertFalse(StockTransfer.objects.exists())

    def test_below_and_exactly_available_are_accepted_as_pending_with_no_movement(self):
        for qty in (15, 16):
            transfer = create_warehouse_floor_transfer(
                actor=self.manager, source_branch_id=self.branch.pk,
                destination_branch_id=self.branch.pk,
                items=[{'product_id': self.tuna.pk, 'quantity': qty}],
            )
            self.assertEqual(transfer.status, StockTransfer.Status.PENDING)
        self.assertEqual(self.warehouse_qty(self.tuna), 16)
        self.assertFalse(StockMovement.objects.exists())

    def test_above_available_is_rejected_with_the_available_count(self):
        with self.assertRaises(StockTransferRequestError) as ctx:
            create_warehouse_floor_transfer(
                actor=self.manager, source_branch_id=self.branch.pk,
                destination_branch_id=self.branch.pk,
                items=[{'product_id': self.tuna.pk, 'quantity': 17}],
            )
        self.assertEqual(
            str(ctx.exception),
            'Insufficient warehouse stock for Century Tuna Flakes in Oil 155g. '
            'Only 16 units are available.',
        )
        self.assertFalse(StockTransfer.objects.exists())

    def test_one_invalid_line_creates_no_partial_request(self):
        response = self.submit([(self.sardines, 3), (self.tuna, 50)])
        self.assertEqual(response.status_code, 200)
        self.assertIn('Century Tuna Flakes in Oil 155g', self.flashed(response))
        self.assertNotIn('Sardines', self.flashed(response))
        self.assertFalse(StockTransfer.objects.exists())

    def test_every_invalid_line_is_reported_together(self):
        response = self.submit([(self.sardines, 11), (self.tuna, 17)])
        message = self.flashed(response)
        self.assertIn('Only 10 units are available', message)
        self.assertIn('Only 16 units are available', message)


class RefillApprovalAndReleaseTests(_RefillFixture, TestCase):
    def setUp(self):
        self.build()
        self.transfer = create_warehouse_floor_transfer(
            actor=self.manager, source_branch_id=self.branch.pk,
            destination_branch_id=self.branch.pk,
            items=[{'product_id': self.tuna.pk, 'quantity': 10}],
        )
        self.client.force_login(self.manager)

    def _approve(self, qty):
        return self.client.post(
            reverse('transfer_approve', args=[self.transfer.pk]),
            {'action': 'approve', 'quantity_approved': [qty]}, follow=True,
        )

    def _release(self):
        item = self.transfer.items.get()
        return self.client.post(
            reverse('transfer_complete', args=[self.transfer.pk]),
            {'item_id': [item.pk], 'quantity_released': [item.quantity_approved],
             'release_method': ['Manual']}, follow=True,
        )

    def _drain_warehouse_to(self, qty):
        ws = WarehouseStock.objects.get(warehouse=self.branch.warehouse, product=self.tuna)
        ws.quantity_on_hand = qty
        ws.save()
        Batch.objects.filter(
            product=self.tuna, branch=self.branch, location=Batch.Location.WAREHOUSE,
        ).update(quantity=qty)

    def test_stock_drop_between_request_and_approval_blocks_approval(self):
        self._drain_warehouse_to(4)
        response = self._approve(10)
        self.assertIn('Not enough warehouse stock', self.flashed(response))
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, StockTransfer.Status.PENDING)

    def test_stock_drop_between_approval_and_release_rolls_everything_back(self):
        self._approve(10)
        self._drain_warehouse_to(4)
        response = self._release()
        self.assertIn('Not enough warehouse stock', self.flashed(response))
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, StockTransfer.Status.APPROVED)
        self.assertEqual(self.warehouse_qty(self.tuna), 4)  # never negative
        self.assertEqual(self.floor_qty(self.tuna), 0)
        self.assertFalse(StockMovement.objects.exists())

    def test_release_with_missing_warehouse_row_is_a_message_not_a_404(self):
        self._approve(10)
        WarehouseStock.objects.filter(product=self.tuna).delete()
        response = self._release()
        self.assertEqual(response.status_code, 200)
        self.assertIn('no longer in warehouse stock', self.flashed(response))

    def test_successful_release_moves_stock_once_and_audits_both_sides(self):
        self._approve(10)
        self.assertEqual(self.warehouse_qty(self.tuna), 16)  # approval moves nothing
        response = self._release()
        self.assertIn('moved', self.flashed(response))
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, StockTransfer.Status.RELEASED)
        # Same branch on both sides: warehouse down, floor up, independently.
        self.assertEqual(self.warehouse_qty(self.tuna), 6)
        self.assertEqual(self.floor_qty(self.tuna), 10)
        movements = StockMovement.objects.filter(reference_id=self.transfer.pk)
        self.assertEqual(
            sorted(movements.values_list('location_type', 'quantity_change')),
            [('Floor', 10), ('Warehouse', -10)],
        )
        # A second release attempt cannot double-move.
        self._release()
        self.assertEqual(self.warehouse_qty(self.tuna), 6)
        self.assertEqual(self.floor_qty(self.tuna), 10)
