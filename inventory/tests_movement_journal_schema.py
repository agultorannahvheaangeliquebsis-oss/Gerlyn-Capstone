"""P03 - StockMovement journal columns (product, branch, reference_type).

Covers what StockMovement.save() fills in for every writer, that a movement
keeps its product/branch after the stock row is deleted, and the data
migration that backfills rows written before the columns existed.
"""
import importlib
from datetime import date, timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.test import TestCase

from branches.models import Branch
from inventory.models import (
    Batch, Inventory, Product, StockMovement, WarehouseStock,
)
from inventory.services import credit_floor_stock, expire_due_batches

MT = StockMovement.MovementType
RT = StockMovement.ReferenceType

backfill = importlib.import_module(
    'inventory.migrations.0031_backfill_stockmovement_journal'
).backfill_journal_columns


class _JournalFixture(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(branch_name='Tarcan', location='Baliuag')
        self.product = Product.objects.create(
            sku_code='SKU-J1', product_name='Rice', unit_price=Decimal('50.00'),
        )
        self.inv = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=10,
        )
        self.ws = WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=self.product,
            quantity_on_hand=20,
        )

    def floor_move(self, movement_type=MT.SALE, **extra):
        return StockMovement.objects.create(
            inventory=self.inv, location_type=StockMovement.LocationType.FLOOR,
            movement_type=movement_type, quantity_change=-1,
            quantity_before=10, quantity_after=9, **extra,
        )

    def warehouse_move(self, movement_type=MT.PROCUREMENT_RECEIVED, **extra):
        return StockMovement.objects.create(
            warehouse_stock=self.ws,
            location_type=StockMovement.LocationType.WAREHOUSE,
            movement_type=movement_type, quantity_change=5,
            quantity_before=20, quantity_after=25, **extra,
        )

    @staticmethod
    def legacy(*movements):
        """Blank the journal columns, as they are on a pre-P03 row."""
        StockMovement.objects.filter(pk__in=[m.pk for m in movements]).update(
            product=None, branch=None, reference_type='',
        )


class ProductBranchDerivationTests(_JournalFixture):
    def test_floor_movement_gets_product_and_branch(self):
        m = self.floor_move()
        self.assertEqual((m.product_id, m.branch_id), (self.product.pk, self.branch.pk))

    def test_warehouse_movement_gets_product_and_branch_via_the_warehouse(self):
        m = self.warehouse_move()
        self.assertEqual((m.product_id, m.branch_id), (self.product.pk, self.branch.pk))

    def test_movement_keeps_product_and_branch_after_the_stock_row_is_deleted(self):
        floor, ware = self.floor_move(), self.warehouse_move()
        self.inv.delete()
        self.ws.delete()
        for m in (floor, ware):
            m.refresh_from_db()
            self.assertIsNone(m.inventory_id or m.warehouse_stock_id)
            self.assertEqual((m.product_id, m.branch_id),
                             (self.product.pk, self.branch.pk))

    def test_explicit_values_are_not_overwritten(self):
        other = Branch.objects.create(branch_name='Makinabang', location='Baliuag')
        m = self.floor_move(branch=other)
        self.assertEqual(m.branch_id, other.pk)
        self.assertEqual(m.product_id, self.product.pk)

    def test_batch_supplies_product_and_branch_when_there_is_no_stock_row(self):
        lot = Batch.objects.create(
            product=self.product, branch=self.branch,
            location=Batch.Location.FLOOR, quantity=3, received_date=date.today(),
        )
        m = StockMovement.objects.create(
            batch=lot, movement_type=MT.EXPIRY_WRITE_OFF, quantity_change=-3,
            quantity_before=3, quantity_after=0,
        )
        self.assertEqual((m.product_id, m.branch_id), (self.product.pk, self.branch.pk))


class ReferenceTypeTests(_JournalFixture):
    def test_type_is_inferred_from_movement_type_when_a_reference_is_given(self):
        expected = {
            MT.SALE: RT.SALES_TRANSACTION,
            MT.CUSTOMER_RETURN: RT.SALES_TRANSACTION,
            MT.PROCUREMENT_RECEIVED: RT.PROCUREMENT,
            MT.SUPPLIER_RETURN: RT.SUPPLIER_RETURN,
            MT.WAREHOUSE_TO_FLOOR: RT.STOCK_TRANSFER,
            MT.FLOOR_TO_WAREHOUSE: RT.STOCK_TRANSFER,
            MT.MANUAL_OUT: RT.STOCK_TRANSFER,
            MT.EXPIRY_WRITE_OFF: RT.BATCH,
            MT.OPENING_BALANCE: RT.OPENING_STOCK_IMPORT,
            MT.OPENING_BALANCE_REVERSAL: RT.OPENING_STOCK_IMPORT,
        }
        for movement_type, reference_type in expected.items():
            with self.subTest(movement_type):
                self.assertEqual(
                    self.floor_move(movement_type, reference_id=7).reference_type,
                    reference_type)

    def test_no_reference_id_means_no_reference_type(self):
        self.assertEqual(self.floor_move(MT.SALE).reference_type, '')

    def test_adjustment_is_never_guessed(self):
        self.assertEqual(self.floor_move(MT.ADJUSTMENT, reference_id=7).reference_type, '')

    def test_an_explicit_type_wins(self):
        m = self.floor_move(
            MT.ADJUSTMENT, reference_id=7, reference_type=RT.STOCK_TRANSFER)
        self.assertEqual(m.reference_type, RT.STOCK_TRANSFER)

    def test_manual_adjustment_writer_records_no_reference(self):
        credit_floor_stock(
            product=self.product, branch=self.branch, quantity=4, actor=None,
            movement_type=MT.ADJUSTMENT, notes='Manual stock adjustment',
        )
        m = StockMovement.objects.get()
        self.assertEqual((m.reference_type, m.reference_id), ('', None))
        self.assertEqual((m.product_id, m.branch_id), (self.product.pk, self.branch.pk))

    def test_expiry_writer_records_the_batch(self):
        lot = Batch.objects.create(
            product=self.product, branch=self.branch,
            location=Batch.Location.FLOOR, quantity=2,
            received_date=date.today() - timedelta(days=30),
            expiry_date=date.today() - timedelta(days=1),
        )
        expire_due_batches(actor=None)
        m = StockMovement.objects.get(movement_type=MT.EXPIRY_WRITE_OFF)
        self.assertEqual((m.reference_type, m.reference_id), (RT.BATCH, lot.pk))
        self.assertEqual((m.product_id, m.branch_id), (self.product.pk, self.branch.pk))


class BackfillMigrationTests(_JournalFixture):
    def run_backfill(self):
        backfill(django_apps, None)

    def test_backfills_product_and_branch_for_floor_and_warehouse_rows(self):
        floor, ware = self.floor_move(), self.warehouse_move()
        self.legacy(floor, ware)
        self.run_backfill()
        for m in (floor, ware):
            m.refresh_from_db()
            self.assertEqual((m.product_id, m.branch_id),
                             (self.product.pk, self.branch.pk))

    def test_orphaned_row_recovers_from_its_batch_or_stays_empty(self):
        lot = Batch.objects.create(
            product=self.product, branch=self.branch,
            location=Batch.Location.FLOOR, quantity=1, received_date=date.today(),
        )
        with_lot = self.floor_move(batch=lot)
        without_lot = self.floor_move()
        self.legacy(with_lot, without_lot)
        self.inv.delete()  # both rows lose their stock row (SET_NULL)
        self.run_backfill()
        with_lot.refresh_from_db()
        without_lot.refresh_from_db()
        self.assertEqual((with_lot.product_id, with_lot.branch_id),
                         (self.product.pk, self.branch.pk))
        self.assertEqual((without_lot.product_id, without_lot.branch_id), (None, None))

    def test_reference_type_is_backfilled_from_movement_type(self):
        sale = self.floor_move(MT.SALE, reference_id=1)
        receipt = self.warehouse_move(MT.PROCUREMENT_RECEIVED, reference_id=2)
        supplier_return = self.warehouse_move(MT.SUPPLIER_RETURN, reference_id=3)
        self.legacy(sale, receipt, supplier_return)
        self.run_backfill()
        got = {m.pk: StockMovement.objects.get(pk=m.pk).reference_type
               for m in (sale, receipt, supplier_return)}
        self.assertEqual(got, {
            sale.pk: RT.SALES_TRANSACTION,
            receipt.pk: RT.PROCUREMENT,
            supplier_return.pk: RT.SUPPLIER_RETURN,
        })

    def test_adjustments_are_typed_by_note_and_otherwise_left_blank(self):
        void = self.floor_move(
            MT.ADJUSTMENT, reference_id=1, notes='Voided order ORD-1 — stock reversal')
        flag = self.floor_move(
            MT.ADJUSTMENT, reference_id=2, notes='Discrepancy on WS-9: short')
        manual = self.floor_move(MT.ADJUSTMENT, notes='Manual stock adjustment by x')
        unknown = self.floor_move(MT.ADJUSTMENT, reference_id=4, notes='something else')
        self.legacy(void, flag, manual, unknown)
        self.run_backfill()
        got = [StockMovement.objects.get(pk=m.pk).reference_type
               for m in (void, flag, manual, unknown)]
        self.assertEqual(got, [RT.SALES_TRANSACTION, RT.STOCK_TRANSFER, '', ''])

    def test_rows_without_a_reference_id_get_no_type(self):
        m = self.floor_move(MT.SALE)
        self.legacy(m)
        self.run_backfill()
        m.refresh_from_db()
        self.assertEqual(m.reference_type, '')

    def test_it_is_idempotent_and_never_overwrites_a_set_value(self):
        m = self.floor_move(
            MT.ADJUSTMENT, reference_id=5, reference_type=RT.STOCK_TRANSFER,
            notes='Voided order ORD-2')
        self.run_backfill()
        self.run_backfill()
        m.refresh_from_db()
        self.assertEqual(m.reference_type, RT.STOCK_TRANSFER)
