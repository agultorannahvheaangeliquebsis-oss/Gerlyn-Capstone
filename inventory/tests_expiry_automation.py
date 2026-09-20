from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from branches.models import Branch
from inventory.models import (
    Batch, Inventory, Product, StockMovement, Warehouse, WarehouseStock,
)
from inventory.serializers import StockMovementSerializer
from inventory.services import ExpiryProcessingError, expire_due_batches


class ExpiryAutomationTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.branch = Branch.objects.create(
            branch_name='Expiry Test Branch', location='Baliuag',
        )
        self.warehouse = Warehouse.objects.get(branch=self.branch)
        self.product = Product.objects.create(
            sku_code='SKU-EXP-AUTO',
            product_name='Expiry Test Product',
            unit_price=Decimal('25.00'),
        )

    def make_batch(self, *, location, quantity, expiry_date, lot=''):
        return Batch.objects.create(
            product=self.product,
            branch=self.branch,
            location=location,
            quantity=quantity,
            received_date=self.today - timedelta(days=10),
            expiry_date=expiry_date,
            external_lot_code=lot,
            notes='Original receiving record',
        )

    def test_expiry_date_is_immediately_non_sellable(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=15,
        )
        due = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
        )
        fresh = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=10,
            expiry_date=self.today + timedelta(days=1),
        )

        inventory = Inventory.objects.sellable().get(
            product=self.product, branch=self.branch,
        )

        self.assertEqual(inventory.available_qty, 10)
        self.assertEqual(due.expiry_status, Batch.ExpiryStatus.EXPIRED)
        self.assertEqual(fresh.expiry_status, Batch.ExpiryStatus.EXPIRING_SOON)

    def test_fefo_never_consumes_an_expired_lot(self):
        expired = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
        )
        fresh = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=10,
            expiry_date=self.today + timedelta(days=2),
        )

        with transaction.atomic():
            consumed = Batch.consume_fefo(
                self.product, self.branch, Batch.Location.FLOOR, 3,
            )

        expired.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual([(batch.pk, qty) for batch, qty in consumed], [(fresh.pk, 3)])
        self.assertEqual(expired.quantity, 5)
        self.assertEqual(fresh.quantity, 7)

    def test_sweep_removes_floor_and_warehouse_lots_and_audits_each_batch(self):
        inventory = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=15,
        )
        warehouse_stock = WarehouseStock.objects.create(
            warehouse=self.warehouse, product=self.product, quantity_on_hand=7,
        )
        floor_due = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
            lot='LOT-FLOOR-1',
        )
        fresh = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=10,
            expiry_date=self.today + timedelta(days=30),
        )
        warehouse_due = self.make_batch(
            location=Batch.Location.WAREHOUSE,
            quantity=7,
            expiry_date=self.today - timedelta(days=1),
            lot='LOT-WH-1',
        )

        result = expire_due_batches(as_of_date=self.today)

        self.assertEqual(result.batch_count, 2)
        self.assertEqual(result.unit_count, 12)
        self.assertEqual(result.floor_unit_count, 5)
        self.assertEqual(result.warehouse_unit_count, 7)
        inventory.refresh_from_db()
        warehouse_stock.refresh_from_db()
        floor_due.refresh_from_db()
        warehouse_due.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(inventory.quantity_on_hand, 10)
        self.assertEqual(warehouse_stock.quantity_on_hand, 0)
        self.assertEqual(floor_due.quantity, 0)
        self.assertEqual(warehouse_due.quantity, 0)
        self.assertEqual(fresh.quantity, 10)
        self.assertEqual(floor_due.notes, 'Original receiving record')
        self.assertEqual(floor_due.external_lot_code, 'LOT-FLOOR-1')

        movements = StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
        ).order_by('batch_id')
        self.assertEqual(movements.count(), 2)
        floor_movement = movements.get(batch=floor_due)
        self.assertEqual(floor_movement.reference_id, floor_due.pk)
        self.assertEqual(floor_movement.quantity_change, -5)
        self.assertEqual(floor_movement.quantity_before, 15)
        self.assertEqual(floor_movement.quantity_after, 10)
        self.assertIsNone(floor_movement.created_by)
        self.assertIn('LOT-FLOOR-1', floor_movement.notes)
        audit_data = StockMovementSerializer(floor_movement).data
        self.assertEqual(audit_data['batchId'], floor_due.pk)
        self.assertEqual(audit_data['batchNumber'], floor_due.batch_number)
        self.assertEqual(audit_data['externalLotCode'], 'LOT-FLOOR-1')
        self.assertEqual(audit_data['batchExpiryDate'], self.today.isoformat())

        second = expire_due_batches(as_of_date=self.today)
        self.assertEqual(second.batch_count, 0)
        self.assertEqual(StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
        ).count(), 2)

    def test_mismatch_aborts_without_partial_disposal(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=5,
        )
        first = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=2,
            expiry_date=self.today,
        )
        second = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
        )

        with self.assertRaises(ExpiryProcessingError):
            expire_due_batches(as_of_date=self.today)

        first.refresh_from_db()
        second.refresh_from_db()
        inventory = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inventory.quantity_on_hand, 5)
        self.assertEqual(first.quantity, 2)
        self.assertEqual(second.quantity, 5)
        self.assertFalse(StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
        ).exists())

    def test_daily_command_runs_the_expiry_sweep(self):
        inventory = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=4,
        )
        batch = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=4,
            expiry_date=self.today,
        )
        output = StringIO()

        call_command('dispatch_notifications', stdout=output)

        inventory.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(inventory.quantity_on_hand, 0)
        self.assertEqual(batch.quantity, 0)
        self.assertIn('Wrote off 1 expired batch (4 units).', output.getvalue())
