"""Actual Received Cost — ProcurementItem.received_line_total and
Procurement.actual_received_cost.

total_cost stays the committed/ordered PO value; line_total stays keyed to
ordered (or, for unexpected lines, ordered-quantity-shaped) quantity. Neither
answers "what did we actually pay for what showed up" when a delivery is
partial, over-received, or includes unexpected items — that's what these two
new read-only properties are for. Both are computed on every access, never
persisted, and never gated on receipt_flag.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from branches.models import Branch
from inventory.models import Product
from procurement.models import Procurement, ProcurementItem


class ActualReceivedCostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='arc-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='ARC Branch', location='Baliuag', owner=cls.owner,
        )
        cls.supplier = User.objects.create_user(
            username='arc-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.product = Product.objects.create(
            sku_code='SKU-ARC-1', product_name='Item', unit_price=Decimal('10.00'),
        )

    def _po(self, total_cost=Decimal('0')):
        return Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.APPROVED, total_cost=total_cost,
        )

    def test_normal_item_received_line_total_uses_received_quantity_and_cost(self):
        item = ProcurementItem.objects.create(
            procurement=self._po(), product=self.product,
            quantity_ordered=10, quantity_received=10,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertEqual(item.received_line_total, Decimal('50.00'))

    def test_normal_item_partial_receipt_only_values_received_quantity(self):
        item = ProcurementItem.objects.create(
            procurement=self._po(), product=self.product,
            quantity_ordered=10, quantity_received=4,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertEqual(item.received_line_total, Decimal('20.00'))
        # line_total is unaffected — it stays keyed to quantity_ordered.
        self.assertEqual(item.line_total, Decimal('50.00'))

    def test_normal_item_zero_received_quantity_is_zero(self):
        item = ProcurementItem.objects.create(
            procurement=self._po(), product=self.product,
            quantity_ordered=10, quantity_received=0,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertEqual(item.received_line_total, 0)

    def test_unexpected_item_with_known_cost(self):
        item = ProcurementItem.objects.create(
            procurement=self._po(), product=self.product,
            quantity_ordered=0, quantity_received=3,
            unit_cost_snapshot=Decimal('7.50'), is_unexpected=True,
        )
        self.assertEqual(item.received_line_total, Decimal('22.50'))

    def test_unexpected_item_with_unrecorded_cost_contributes_zero(self):
        item = ProcurementItem.objects.create(
            procurement=self._po(), product=self.product,
            quantity_ordered=0, quantity_received=5,
            unit_cost_snapshot=None, is_unexpected=True,
        )
        # Zero because the cost is unknown, not because the item was free.
        self.assertEqual(item.received_line_total, 0)

    def test_actual_received_cost_sums_multiple_items(self):
        po = self._po()
        ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=10, quantity_received=10,
            unit_cost_snapshot=Decimal('5.00'),
        )
        ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=5, quantity_received=2,
            unit_cost_snapshot=Decimal('3.00'),
        )
        ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=0, quantity_received=4,
            unit_cost_snapshot=Decimal('2.00'), is_unexpected=True,
        )
        # 50.00 + 6.00 + 8.00
        self.assertEqual(po.actual_received_cost, Decimal('64.00'))

    def test_actual_received_cost_is_dynamic_not_persisted(self):
        po = self._po()
        item = ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=10, quantity_received=4,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertEqual(po.actual_received_cost, Decimal('20.00'))

        item.quantity_received = 10
        item.save(update_fields=['quantity_received'])

        # Re-fetch to prove the value is recomputed from current rows, not
        # cached on the Procurement instance or in the database.
        po.refresh_from_db()
        self.assertEqual(po.actual_received_cost, Decimal('50.00'))
        self.assertNotIn('actual_received_cost', po.__dict__)

    def test_line_total_behavior_is_unchanged(self):
        normal = ProcurementItem.objects.create(
            procurement=self._po(), product=self.product,
            quantity_ordered=10, quantity_received=4,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertEqual(normal.line_total, Decimal('50.00'))

        unexpected = ProcurementItem.objects.create(
            procurement=self._po(), product=self.product,
            quantity_ordered=0, quantity_received=3,
            unit_cost_snapshot=Decimal('7.50'), is_unexpected=True,
        )
        self.assertEqual(unexpected.line_total, Decimal('22.50'))

    def test_procurement_total_cost_is_unchanged_by_receiving(self):
        po = self._po(total_cost=Decimal('50.00'))
        ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=10, quantity_received=4,
            unit_cost_snapshot=Decimal('5.00'),
        )
        po.refresh_from_db()
        self.assertEqual(po.total_cost, Decimal('50.00'))
        self.assertNotEqual(po.total_cost, po.actual_received_cost)


class PoDocumentReceivedNoteTests(TestCase):
    """The printed PO / PDF gain a second, separate figure. It must not
    displace the existing "stored total doesn't add up" integrity warning,
    and it must stay silent on an order that hasn't been received — that
    document is the one sent TO the supplier."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='doc-note-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Doc Note Branch', location='Baliuag', owner=cls.owner,
        )
        cls.supplier = User.objects.create_user(
            username='doc-note-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.product = Product.objects.create(
            sku_code='SKU-DN-1', product_name='Item', unit_price=Decimal('10.00'),
        )

    def _po_with_partial_receipt(self, status):
        po = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            order_date=dt.date(2026, 1, 1), status=status,
            total_cost=Decimal('50.00'),
        )
        ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=10, quantity_received=4,
            unit_cost_snapshot=Decimal('5.00'),
        )
        return po

    def _context(self, po):
        from django.test import RequestFactory
        from procurement.views import _procurement_document_context
        request = RequestFactory().get('/')
        request.user = self.owner
        return _procurement_document_context(po, request)

    def test_an_unreceived_order_never_announces_an_actual_received_total(self):
        for status in (
            Procurement.Status.APPROVED,
            Procurement.Status.INCOMING,
            Procurement.Status.CANCELLED,
        ):
            with self.subTest(status=status):
                context = self._context(self._po_with_partial_receipt(status))
                self.assertFalse(context['received_differs'])

    def test_a_partial_receipt_is_flagged_once_the_order_is_in_a_receipt_state(self):
        for status in (
            Procurement.Status.PARTIALLY_RECEIVED,
            Procurement.Status.RECEIVED,
        ):
            with self.subTest(status=status):
                context = self._context(self._po_with_partial_receipt(status))
                self.assertTrue(context['received_differs'])
                self.assertEqual(context['actual_received_cost'], Decimal('20.00'))

    def test_a_fully_received_order_has_nothing_to_flag(self):
        po = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.RECEIVED, total_cost=Decimal('50.00'),
        )
        ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=10, quantity_received=10,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertFalse(self._context(po)['received_differs'])

    def test_the_stored_total_integrity_warning_is_still_computed_separately(self):
        # total_cost (50.00) disagrees with the line sum (10 x 5 = 50.00
        # after the edit below becomes 30.00) — the original check, untouched.
        po = self._po_with_partial_receipt(Procurement.Status.APPROVED)
        po.items.update(quantity_ordered=6)
        context = self._context(po)
        self.assertTrue(context['totals_disagree'])
        self.assertEqual(context['line_sum'], Decimal('30.00'))
