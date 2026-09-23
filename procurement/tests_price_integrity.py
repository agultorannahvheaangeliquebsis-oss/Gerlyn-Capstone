"""P0 — Enforce Positive Prices and Costs (procurement side).

Covers every place a supplier or negotiated cost is written:
- SupplierProduct.current_cost_price via supplier_set_product_cost (the
  supplier's own "my current cost" form) used to accept ₱0.
- ProcurementItem.proposed_unit_cost via the negotiation views
  (procurement_supplier_respond / procurement_owner_respond) already
  refused a non-positive cost — this locks that in with regression tests.
- The database CheckConstraints backstopping all of the above, plus
  Procurement.total_cost (which stays >= 0, not > 0 — see its own
  CheckConstraint's docstring in procurement.models for why zero is valid
  there).
- The data migration that safely corrects any historical row that
  predates these rules.
"""
import datetime as dt
import importlib
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from inventory.models import Product
from procurement.models import Procurement, ProcurementItem, SupplierProduct


class SupplierSetProductCostValidationTests(TestCase):
    """supplier_set_product_cost — procurement.views."""

    def setUp(self):
        self.supplier = User.objects.create_user(
            username='cost-link-supplier', password='pw', role=User.Role.SUPPLIER
        )
        self.product = Product.objects.create(
            sku_code='SKU-LINK-1', product_name='Item', unit_price=Decimal('10.00'),
        )
        self.link = SupplierProduct.objects.create(
            supplier=self.supplier, product=self.product,
            current_cost_price=Decimal('5.00'),
        )
        self.client.force_login(self.supplier)

    def _set(self, cost):
        return self.client.post(
            reverse('supplier_set_product_cost', args=[self.link.pk]),
            {'current_cost_price': cost},
        )

    def test_zero_cost_is_rejected_and_unchanged(self):
        self._set('0')
        self.link.refresh_from_db()
        self.assertEqual(self.link.current_cost_price, Decimal('5.00'))

    def test_negative_cost_is_rejected_and_unchanged(self):
        self._set('-1.00')
        self.link.refresh_from_db()
        self.assertEqual(self.link.current_cost_price, Decimal('5.00'))

    def test_a_positive_cost_is_accepted(self):
        self._set('7.50')
        self.link.refresh_from_db()
        self.assertEqual(self.link.current_cost_price, Decimal('7.50'))


class NegotiationCostValidationTests(TestCase):
    """procurement_supplier_respond — a zero/negative proposed cost must be
    refused exactly like a missing one already is (see
    procurement.tests.NegotiationFlowTests.test_a_missing_cost_is_rejected_
    and_nothing_changes)."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='cost-neg-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Cost Neg Branch', location='Baliuag', owner=self.owner,
        )
        self.supplier = User.objects.create_user(
            username='cost-neg-supplier', password='pw', role=User.Role.SUPPLIER
        )
        self.product = Product.objects.create(
            sku_code='SKU-NEG-1', product_name='Item', unit_price=Decimal('10.00'),
        )
        self.po = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.APPROVED, total_cost=0,
        )
        self.item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product, quantity_ordered=5,
        )
        self.client.force_login(self.supplier)

    def _propose(self, cost):
        delivery_date = timezone.localdate() + dt.timedelta(days=14)
        return self.client.post(
            reverse('procurement_supplier_respond', args=[self.po.pk]), {
                'action': 'propose',
                'delivery_date': delivery_date.isoformat(),
                f'unit_cost_{self.item.pk}': cost,
            }
        )

    def test_zero_cost_is_rejected_and_nothing_changes(self):
        self._propose('0')
        self.po.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.APPROVED)
        self.assertIsNone(self.item.proposed_unit_cost)

    def test_negative_cost_is_rejected_and_nothing_changes(self):
        self._propose('-2.00')
        self.po.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.APPROVED)
        self.assertIsNone(self.item.proposed_unit_cost)

    def test_a_positive_cost_is_accepted(self):
        self._propose('12.00')
        self.po.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.PENDING_OWNER_REVIEW)
        self.assertEqual(self.item.proposed_unit_cost, Decimal('12.00'))


class ProcurementItemCostConstraintTests(TestCase):
    """Database backstop for any write path that reaches the ORM directly."""

    def setUp(self):
        self.supplier = User.objects.create_user(
            username='cost-item-supplier', password='pw', role=User.Role.SUPPLIER
        )
        self.product = Product.objects.create(
            sku_code='SKU-ITEM-1', product_name='Item', unit_price=Decimal('10.00'),
        )
        self.po = Procurement.objects.create(
            supplier=self.supplier, order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.DRAFT, total_cost=0,
        )

    def test_zero_unit_cost_snapshot_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProcurementItem.objects.create(
                    procurement=self.po, product=self.product, quantity_ordered=1,
                    unit_cost_snapshot=Decimal('0'),
                )

    def test_negative_proposed_unit_cost_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProcurementItem.objects.create(
                    procurement=self.po, product=self.product, quantity_ordered=1,
                    proposed_unit_cost=Decimal('-1.00'),
                )

    def test_null_costs_are_allowed_before_negotiation(self):
        item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product, quantity_ordered=1,
        )
        self.assertIsNone(item.unit_cost_snapshot)
        self.assertIsNone(item.proposed_unit_cost)

    def test_positive_costs_are_allowed(self):
        item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product, quantity_ordered=1,
            unit_cost_snapshot=Decimal('5.00'), proposed_unit_cost=Decimal('5.00'),
        )
        self.assertEqual(item.unit_cost_snapshot, Decimal('5.00'))


class SupplierProductCostConstraintTests(TestCase):
    def setUp(self):
        self.supplier = User.objects.create_user(
            username='cost-sp-supplier', password='pw', role=User.Role.SUPPLIER
        )
        self.product = Product.objects.create(
            sku_code='SKU-SP-1', product_name='Item', unit_price=Decimal('10.00'),
        )

    def test_zero_current_cost_price_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SupplierProduct.objects.create(
                    supplier=self.supplier, product=self.product,
                    current_cost_price=Decimal('0'),
                )

    def test_negative_current_cost_price_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SupplierProduct.objects.create(
                    supplier=self.supplier, product=self.product,
                    current_cost_price=Decimal('-3.00'),
                )

    def test_null_current_cost_price_is_allowed(self):
        link = SupplierProduct.objects.create(
            supplier=self.supplier, product=self.product,
        )
        self.assertIsNone(link.current_cost_price)


class ProcurementTotalCostConstraintTests(TestCase):
    """total_cost is a computed total, not an entered price — zero is valid
    (a fresh Draft), only negative is refused."""

    def setUp(self):
        self.supplier = User.objects.create_user(
            username='cost-total-supplier', password='pw', role=User.Role.SUPPLIER
        )

    def test_negative_total_cost_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Procurement.objects.create(
                    supplier=self.supplier, order_date=dt.date(2026, 1, 1),
                    status=Procurement.Status.DRAFT, total_cost=Decimal('-1.00'),
                )

    def test_zero_total_cost_is_allowed(self):
        po = Procurement.objects.create(
            supplier=self.supplier, order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.DRAFT, total_cost=Decimal('0'),
        )
        self.assertEqual(po.total_cost, Decimal('0.00'))


class InvalidHistoricalCostMigrationTests(TransactionTestCase):
    """0008_enforce_positive_prices's data-fix step
    (fix_invalid_procurement_costs) must safely correct historical rows
    that predate these rules — see InvalidHistoricalPriceMigrationTests in
    inventory.tests_price_validation for why the constraints have to be
    temporarily dropped to reproduce that "already invalid" starting state
    on a database that now enforces them at write time."""

    def _drop(self, model, name):
        constraint = next(c for c in model._meta.constraints if c.name == name)
        with connection.schema_editor() as editor:
            editor.remove_constraint(model, constraint)
        return constraint

    def _restore(self, model, constraint):
        with connection.schema_editor() as editor:
            editor.add_constraint(model, constraint)

    def test_invalid_rows_are_corrected(self):
        migration = importlib.import_module(
            'procurement.migrations.0008_enforce_positive_prices'
        )
        from django.apps import apps as real_apps

        supplier = User.objects.create_user(
            username='cost-migrate-supplier', password='pw', role=User.Role.SUPPLIER
        )
        product = Product.objects.create(
            sku_code='SKU-COST-MIGRATE-1', product_name='Item',
            unit_price=Decimal('10.00'),
        )

        constraints = [
            (Procurement, 'procurement_total_cost_not_negative'),
            (ProcurementItem, 'procurementitem_unit_cost_snapshot_positive_or_null'),
            (ProcurementItem, 'procurementitem_proposed_unit_cost_positive_or_null'),
            (SupplierProduct, 'supplierproduct_current_cost_price_positive_or_null'),
        ]
        dropped = [(model, self._drop(model, name)) for model, name in constraints]
        try:
            po = Procurement.objects.create(
                supplier=supplier, order_date=dt.date(2026, 1, 1),
                status=Procurement.Status.DRAFT, total_cost=Decimal('0'),
            )
            Procurement.objects.filter(pk=po.pk).update(total_cost=Decimal('-25.00'))

            item = ProcurementItem.objects.create(
                procurement=po, product=product, quantity_ordered=2,
            )
            ProcurementItem.objects.filter(pk=item.pk).update(
                unit_cost_snapshot=Decimal('-3.00'), proposed_unit_cost=Decimal('-4.00'),
            )

            link = SupplierProduct.objects.create(supplier=supplier, product=product)
            SupplierProduct.objects.filter(pk=link.pk).update(
                current_cost_price=Decimal('-1.00')
            )

            # The fix must run — and succeed — before the constraints can be
            # restored, exactly the order the real migration applies them in.
            migration.fix_invalid_procurement_costs(real_apps, None)
        finally:
            for model, constraint in dropped:
                self._restore(model, constraint)

        po.refresh_from_db()
        item.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(po.total_cost, Decimal('0.00'))
        self.assertEqual(item.unit_cost_snapshot, Decimal('0.01'))
        self.assertEqual(item.proposed_unit_cost, Decimal('0.01'))
        self.assertEqual(link.current_cost_price, Decimal('0.01'))

    def test_valid_and_null_values_are_left_untouched(self):
        migration = importlib.import_module(
            'procurement.migrations.0008_enforce_positive_prices'
        )
        from django.apps import apps as real_apps

        supplier = User.objects.create_user(
            username='cost-migrate-supplier-2', password='pw', role=User.Role.SUPPLIER
        )
        product = Product.objects.create(
            sku_code='SKU-COST-MIGRATE-2', product_name='Item',
            unit_price=Decimal('10.00'),
        )
        po = Procurement.objects.create(
            supplier=supplier, order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.DRAFT, total_cost=Decimal('40.00'),
        )
        item = ProcurementItem.objects.create(
            procurement=po, product=product, quantity_ordered=4,
            unit_cost_snapshot=Decimal('10.00'),
        )
        link = SupplierProduct.objects.create(supplier=supplier, product=product)

        migration.fix_invalid_procurement_costs(real_apps, None)

        po.refresh_from_db()
        item.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(po.total_cost, Decimal('40.00'))
        self.assertEqual(item.unit_cost_snapshot, Decimal('10.00'))
        self.assertIsNone(item.proposed_unit_cost)
        self.assertIsNone(link.current_cost_price)
