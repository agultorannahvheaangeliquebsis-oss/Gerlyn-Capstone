"""KATRINA-ALERT-001 — the sidebar/bottom-nav Alerts badge must count
everything the Alerts Center page itself surfaces, not floor low stock
alone. See inventory.alerts.urgent_alert_count and
core.context_processors.alerts.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from inventory.alerts import urgent_alert_count
from inventory.models import Batch, Inventory, Product, Warehouse, WarehouseStock
from procurement.models import Procurement


class UrgentAlertCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.branch = Branch.objects.create(branch_name='Alert Branch', location='Baliuag')
        # Branch's own post_save signal already creates one — see
        # branches/signals.py.
        cls.warehouse, _ = Warehouse.objects.get_or_create(
            branch=cls.branch, defaults={'name': 'Main Warehouse'},
        )
        cls.supplier = User.objects.create_user(
            username='alert-supplier', password='pw', role=User.Role.SUPPLIER,
        )

    def _product(self, sku):
        return Product.objects.create(
            sku_code=sku, product_name=sku, unit_price=Decimal('10.00'),
        )

    def test_zero_when_nothing_needs_attention(self):
        product = self._product('SKU-OK')
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=100, reorder_level=10,
        )
        self.assertEqual(urgent_alert_count([self.branch.id]), 0)

    def test_counts_floor_low_stock(self):
        product = self._product('SKU-FLOOR-LOW')
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=5, reorder_level=10,
        )
        self.assertEqual(urgent_alert_count([self.branch.id]), 1)

    def test_counts_warehouse_low_stock_even_with_no_floor_alert(self):
        """Pre-fix, this returned 0 — the badge only ever read floor stock."""
        product = self._product('SKU-WH-LOW')
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=100, reorder_level=10,
        )
        WarehouseStock.objects.create(
            warehouse=self.warehouse, product=product,
            quantity_on_hand=2, reorder_level=10,
        )
        self.assertEqual(urgent_alert_count([self.branch.id]), 1)

    def test_counts_expired_expiring_and_approaching_batches(self):
        """Pre-fix, this returned 0 — expiry alerts never reached the badge."""
        product = self._product('SKU-EXPIRY')
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=30, reorder_level=5,
        )
        Batch.objects.create(
            product=product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=10, received_date=self.today - dt.timedelta(days=5),
            expiry_date=self.today - dt.timedelta(days=1),  # Expired
        )
        Batch.objects.create(
            product=product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=10, received_date=self.today - dt.timedelta(days=5),
            expiry_date=self.today + dt.timedelta(days=10),  # Expiring Soon
        )
        Batch.objects.create(
            product=product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=10, received_date=self.today - dt.timedelta(days=5),
            expiry_date=self.today + dt.timedelta(days=60),  # Approaching Expiry
        )
        self.assertEqual(urgent_alert_count([self.branch.id]), 3)

    def test_counts_overdue_deliveries(self):
        """Pre-fix, this returned 0 — overdue POs never reached the badge."""
        Procurement.objects.create(
            branch=self.branch, supplier=self.supplier,
            order_date=self.today - dt.timedelta(days=20),
            status=Procurement.Status.INCOMING, total_cost=0,
            delivery_date=self.today - dt.timedelta(days=2),
        )
        self.assertEqual(urgent_alert_count([self.branch.id]), 1)

    def test_does_not_double_count_critical_rows(self):
        """critical_count is a severity TAG on a subset of low_count/
        warehouse_low_count, not a distinct group of rows — must not be
        added on top of them."""
        product = self._product('SKU-CRITICAL')
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=1, reorder_level=10,  # well under 50% -> Critical
        )
        self.assertEqual(urgent_alert_count([self.branch.id]), 1)

    def test_branch_scoping_excludes_another_branchs_alerts(self):
        other_branch = Branch.objects.create(branch_name='Other Branch', location='Makinabang')
        product = self._product('SKU-OTHER-BRANCH')
        Inventory.objects.create(
            product=product, branch=other_branch,
            quantity_on_hand=1, reorder_level=10,
        )
        self.assertEqual(urgent_alert_count([self.branch.id]), 0)
        self.assertEqual(urgent_alert_count([other_branch.id]), 1)

    def test_none_branch_ids_means_every_branch(self):
        product = self._product('SKU-ALL-BRANCHES')
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=1, reorder_level=10,
        )
        self.assertGreaterEqual(urgent_alert_count(None), 1)


class AlertBadgeContextProcessorTests(TestCase):
    """The sidebar badge (`alert_count`) itself, end to end over a real
    request, not just the underlying helper."""

    def setUp(self):
        self.branch = Branch.objects.create(branch_name='Badge Branch', location='Baliuag')
        self.warehouse, _ = Warehouse.objects.get_or_create(
            branch=self.branch, defaults={'name': 'Main Warehouse'},
        )
        self.owner = User.objects.create_user(
            username='badge-owner', password='pw', role=User.Role.OWNER,
        )
        self.client.force_login(self.owner)

    def test_badge_counts_a_warehouse_alert_with_no_floor_alert(self):
        product = Product.objects.create(
            sku_code='SKU-BADGE-WH', product_name='Badge WH', unit_price=Decimal('10.00'),
        )
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=100, reorder_level=10,
        )
        WarehouseStock.objects.create(
            warehouse=self.warehouse, product=product,
            quantity_on_hand=1, reorder_level=10,
        )
        response = self.client.get(reverse('accounts:owner_dashboard'))
        self.assertEqual(response.context['alert_count'], 1)
