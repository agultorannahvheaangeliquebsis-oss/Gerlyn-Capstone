"""P1 — ProcurementItem.line_total must price unexpected receiving lines.

Unexpected items (created during receiving with is_unexpected=True) are
saved with quantity_ordered=0 and quantity_received>0. line_total used to
always multiply by quantity_ordered, so every unexpected line displayed as
PHP 0.00 even after it had a real unit_cost_snapshot. Fixed to use
quantity_received for unexpected items and quantity_ordered otherwise.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from branches.models import Branch
from inventory.models import Product
from procurement.models import Procurement, ProcurementItem


class ProcurementItemLineTotalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='line-total-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Line Total Branch', location='Baliuag', owner=cls.owner,
        )
        cls.supplier = User.objects.create_user(
            username='line-total-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.product = Product.objects.create(
            sku_code='SKU-LT-1', product_name='Item', unit_price=Decimal('10.00'),
        )
        cls.po = Procurement.objects.create(
            branch=cls.branch, supplier=cls.supplier, owner=cls.owner,
            order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.APPROVED, total_cost=0,
        )

    def test_normal_ordered_item_uses_quantity_ordered(self):
        item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product,
            quantity_ordered=10, quantity_received=0,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertEqual(item.line_total, Decimal('50.00'))

    def test_normal_item_line_total_follows_received_quantity_change(self):
        # Not unexpected, so line_total is still keyed to quantity_ordered
        # (the agreed PO quantity) even after partial receiving updates
        # quantity_received.
        item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product,
            quantity_ordered=10, quantity_received=4,
            unit_cost_snapshot=Decimal('5.00'),
        )
        self.assertEqual(item.line_total, Decimal('50.00'))

    def test_unexpected_item_with_known_unit_cost_uses_quantity_received(self):
        item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product,
            quantity_ordered=0, quantity_received=3,
            unit_cost_snapshot=Decimal('7.50'), is_unexpected=True,
        )
        self.assertEqual(item.line_total, Decimal('22.50'))

    def test_unexpected_item_with_no_unit_cost_is_zero(self):
        item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product,
            quantity_ordered=0, quantity_received=3,
            unit_cost_snapshot=None, is_unexpected=True,
        )
        self.assertEqual(item.line_total, 0)

    def test_unexpected_item_line_total_follows_received_quantity_change(self):
        item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product,
            quantity_ordered=0, quantity_received=2,
            unit_cost_snapshot=Decimal('4.00'), is_unexpected=True,
        )
        self.assertEqual(item.line_total, Decimal('8.00'))

        item.quantity_received = 6
        item.save(update_fields=['quantity_received'])
        self.assertEqual(item.line_total, Decimal('24.00'))
