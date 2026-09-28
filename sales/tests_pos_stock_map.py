"""The Walk-in Sale page must offer the cashier only what the sale check will
accept. _deduct_stock enforces Inventory.sellable() -> available_qty (expired
batches netted out), so the stock map the cart JS caps against has to report
that same figure, not the raw quantity_on_hand."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from inventory.models import Batch, Inventory, Product


class PosStockMapTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='psm-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-PSM', product_name='Cooking Oil (Bottle)',
            unit_of_measure='Bottle', unit_price=120,
        )
        self.client.force_login(self.owner)

    def _stock_entry(self):
        response = self.client.get(reverse('transaction_add'), {'branch': self.branch.pk})
        self.assertEqual(response.status_code, 200)
        return response.context['pos_stock_map'][str(self.product.pk)][str(self.branch.pk)]

    def _batch(self, quantity, expiry_date):
        return Batch.objects.create(
            product=self.product, branch=self.branch,
            location=Batch.Location.FLOOR, quantity=quantity,
            received_date=timezone.localdate() - timedelta(days=30),
            expiry_date=expiry_date,
        )

    def test_expired_stock_is_not_offered_to_the_cart(self):
        today = timezone.localdate()
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=11, reorder_level=2,
        )
        self._batch(7, today - timedelta(days=1))  # expired, still in quantity_on_hand
        self._batch(4, today + timedelta(days=90))

        entry = self._stock_entry()

        self.assertEqual(entry['qty'], 4)

    def test_all_expired_reads_as_no_stock(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=5, reorder_level=2,
        )
        self._batch(5, timezone.localdate() - timedelta(days=1))

        entry = self._stock_entry()

        self.assertEqual(entry['qty'], 0)
        self.assertEqual(entry['status'], 'No Stock')

    def test_stock_without_batches_falls_back_to_quantity_on_hand(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=9, reorder_level=2,
        )

        self.assertEqual(self._stock_entry()['qty'], 9)
