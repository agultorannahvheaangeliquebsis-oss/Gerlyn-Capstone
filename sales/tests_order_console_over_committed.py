"""S-11 — the Order Console had no way to see that a branch had promised
more of an item, across its open orders, than its floor stock could cover.
Reporting only: nothing here reserves stock, so this is a count and a flag
to look into, not a hard stop.
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesItem, SalesTransaction


class OrderConsoleOverCommittedTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='console-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Console Branch', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-900', product_name='Sugar (Sack)',
            unit_of_measure='Sack', unit_price=60,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=10, reorder_level=2,
        )
        self.client.force_login(self.owner)

    def _open_order(self, quantity, status=SalesTransaction.OrderStatus.PENDING):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=quantity * 60,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
            order_status=status,
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product,
            quantity_sold=quantity, unit_price=60,
        )
        return txn

    def test_orders_within_floor_stock_are_not_flagged(self):
        txn = self._open_order(6)
        response = self.client.get(reverse('transaction_list'))
        self.assertEqual(response.context['over_committed_count'], 0)
        self.assertNotIn(txn.pk, response.context['over_committed_ids'])
        self.assertNotContains(response, 'Over-committed</span>')

    def test_open_orders_promising_more_than_floor_stock_are_flagged(self):
        # 6 + 6 = 12 promised against 10 on the floor.
        first = self._open_order(6)
        second = self._open_order(6)
        response = self.client.get(reverse('transaction_list'))
        self.assertEqual(response.context['over_committed_count'], 2)
        self.assertIn(first.pk, response.context['over_committed_ids'])
        self.assertIn(second.pk, response.context['over_committed_ids'])
        self.assertContains(response, 'Over-committed</span>')

    def test_completed_orders_are_not_counted_toward_the_commitment(self):
        # A finished sale already left the floor — it isn't still "promised".
        self._open_order(6, status=SalesTransaction.OrderStatus.COMPLETED)
        self._open_order(6, status=SalesTransaction.OrderStatus.COMPLETED)
        response = self.client.get(reverse('transaction_list'))
        self.assertEqual(response.context['over_committed_count'], 0)

    def test_cancelled_orders_are_not_counted_toward_the_commitment(self):
        self._open_order(6)
        self._open_order(6, status=SalesTransaction.OrderStatus.CANCELLED)
        response = self.client.get(reverse('transaction_list'))
        # Only the one open order (6) against 10 floor stock — not flagged.
        self.assertEqual(response.context['over_committed_count'], 0)
