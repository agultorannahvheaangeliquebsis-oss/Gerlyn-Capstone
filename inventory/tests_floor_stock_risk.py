from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from core import module_permissions as mp
from inventory.models import Batch, Inventory, Product


class FloorStockRiskLineTests(TestCase):
    """The Status cell's risk line on Floor Stock: the product/branch total the
    pill was judged on plus inventory.alerts.suggested_reorder_qty, shown only
    on rows that need restocking."""

    def setUp(self):
        self.today = timezone.localdate()
        self.owner = User.objects.create_user(
            username='risk-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(branch_name='Risk Branch', location='Baliuag')
        self.url = reverse('inventory_list')
        self.client.force_login(self.owner)

    def _stock(self, sku, name, reorder_level, *batch_qtys):
        product = Product.objects.create(
            sku_code=sku, product_name=name, unit_price=Decimal('10.00'),
        )
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=sum(batch_qtys), reorder_level=reorder_level,
        )
        for qty in batch_qtys:
            Batch.objects.create(
                product=product, branch=self.branch, location=Batch.Location.FLOOR,
                quantity=qty, received_date=self.today - timedelta(days=1),
            )
        return product

    def test_a_critical_row_shows_the_shelf_total_and_the_suggested_quantity(self):
        # Two lots of 4 and 5: neither Batch Qty says why the shelf is Critical.
        self._stock('SKU-RISK-1', 'Split Lot Product', 20, 4, 5)

        response = self.client.get(self.url)

        rows = response.context['rows']
        self.assertEqual([r['status'] for r in rows], ['Critical', 'Critical'])
        self.assertEqual({r['suggested_qty'] for r in rows}, {31})  # 20*2 - 9
        self.assertContains(response, '9 on hand · +31 suggested', count=2)

    def test_a_low_stock_row_shows_it_too(self):
        self._stock('SKU-RISK-2', 'Near Reorder Product', 20, 21)

        response = self.client.get(self.url)

        self.assertEqual(response.context['rows'][0]['status'], 'Low Stock')
        self.assertContains(response, '21 on hand · +19 suggested')

    def test_a_no_stock_row_with_no_reorder_level_shows_only_the_total(self):
        product = Product.objects.create(
            sku_code='SKU-RISK-3', product_name='Empty Product', unit_price=Decimal('10.00'),
        )
        Inventory.objects.create(product=product, branch=self.branch, quantity_on_hand=0)

        response = self.client.get(self.url)

        self.assertEqual(response.context['rows'][0]['status'], 'No Stock')
        self.assertContains(response, '0 on hand</span>')
        self.assertNotContains(response, 'suggested</span>')

    def test_an_in_stock_row_has_no_risk_line(self):
        self._stock('SKU-RISK-4', 'Plenty Product', 10, 100)

        response = self.client.get(self.url)

        self.assertEqual(response.context['rows'][0]['status'], 'In Stock')
        self.assertEqual(response.context['rows'][0]['suggested_qty'], 0)
        self.assertNotContains(response, 'on hand</span>')
        self.assertNotContains(response, 'Restock list in Alerts')

    def test_the_alerts_link_is_offered_when_something_needs_restocking(self):
        self._stock('SKU-RISK-5', 'Short Product', 20, 5)

        response = self.client.get(self.url)

        self.assertEqual(response.context['restock_count'], 1)
        self.assertContains(response, f'href="{reverse("alerts_center")}"')
        self.assertContains(response, 'Restock list in Alerts')

    def test_the_alerts_link_needs_the_alerts_module(self):
        self._stock('SKU-RISK-6', 'Short Product', 20, 5)
        manager = User.objects.create_user(
            username='risk-manager', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        manager.manager_profile.branch = self.branch
        manager.manager_profile.save()
        manager.allowed_modules = [m for m in mp.ALL_MODULE_CODES if m != mp.ALERTS]
        manager.save()
        self.client.force_login(manager)

        response = self.client.get(self.url)

        # The risk line itself is not gated — it is this page's own data.
        self.assertContains(response, '5 on hand · +35 suggested')
        self.assertNotContains(response, 'Restock list in Alerts')
