"""Alerts Center -> Request Floor Refill / Create Purchase Order prefill.

Opening either form from an alert row must only prefill: nothing is deducted,
created or approved by a GET, and every shown figure is re-read from live
inventory rather than trusted from the link.
"""
from datetime import date

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import (
    Batch, Inventory, Product, StockTransfer, WarehouseStock,
)
from procurement.models import Procurement, SupplierProduct


class AlertPrefillBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='ap-owner', password='pw', role=User.Role.OWNER,
        )
        cls.branch = Branch.objects.create(
            owner=cls.owner, branch_name='Alert Branch', location='A',
        )
        cls.other_branch = Branch.objects.create(
            owner=cls.owner, branch_name='Other Branch', location='B',
        )
        cls.manager = User.objects.create_user(
            username='ap-mgr', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        cls.manager.manager_profile.branch = cls.branch
        cls.manager.manager_profile.save(update_fields=['branch'])

        # Many products so the target is never first alphabetically.
        for i in range(5):
            Product.objects.create(
                sku_code=f'AAA{i}', product_name=f'AAA Filler {i}', unit_price='10.00',
            )
        cls.product = Product.objects.create(
            sku_code='ZZZ', product_name='ZZZ Target Sardines', unit_price='30.00',
        )
        WarehouseStock.objects.create(
            warehouse=cls.branch.warehouse, product=cls.product,
            quantity_on_hand=40, reorder_level=50,
        )
        Batch.objects.create(
            product=cls.product, branch=cls.branch,
            location=Batch.Location.WAREHOUSE, quantity=40,
            received_date=date.today(), expiry_date=date(2030, 1, 1),
        )
        Inventory.objects.create(
            branch=cls.branch, product=cls.product,
            quantity_on_hand=4, reorder_level=20,
        )

    def refill_url(self, **extra):
        params = {
            'open_request': '1', 'branch': self.branch.pk,
            'product': self.product.pk, 'qty': 36, 'current': 4, 'reorder': 20,
        }
        params.update(extra)
        return reverse('warehouse_stock_list') + '?' + '&'.join(
            f'{k}={v}' for k, v in params.items()
        )


class RefillPrefillTests(AlertPrefillBase):
    def test_product_preselected_first_with_suggested_qty(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.refill_url())
        self.assertEqual(response.status_code, 200)
        alert = response.context['request_alert']
        self.assertEqual(alert['product'], self.product)
        self.assertEqual(alert['branch'], self.branch)
        self.assertEqual(alert['current_qty'], 4)
        self.assertEqual(alert['reorder_level'], 20)
        rows = response.context['request_rows']
        self.assertEqual(rows[0]['product'], self.product)
        self.assertEqual(rows[0]['quantity'], 36)

    def test_figures_come_from_live_inventory_not_the_link(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.refill_url(current=999, reorder=1))
        alert = response.context['request_alert']
        self.assertEqual(alert['current_qty'], 4)
        self.assertEqual(alert['reorder_level'], 20)
        self.assertTrue(alert['stale'])

    def test_over_request_is_flagged_not_capped(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.refill_url(qty=500))
        self.assertEqual(response.context['request_rows'][0]['quantity'], 500)
        self.assertTrue(response.context['request_alert']['short'])

    def test_opening_form_moves_no_stock_and_creates_no_request(self):
        self.client.force_login(self.owner)
        self.client.get(self.refill_url())
        self.assertEqual(StockTransfer.objects.count(), 0)
        self.assertEqual(
            WarehouseStock.objects.get(product=self.product).quantity_on_hand, 40,
        )

    def test_manual_open_has_no_alert(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('warehouse_stock_list') + '?open_request=1')
        self.assertIsNone(response.context['request_alert'])

    def test_manager_cannot_retarget_another_branch(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.refill_url(branch=self.other_branch.pk))
        self.assertEqual(response.context['request_target_branch'], self.branch)

    def test_over_quantity_submit_is_rejected_with_message(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('warehouse_stock_request'), {
            'branch_id': self.branch.pk, 'source_branch_id': self.branch.pk,
            'alert_product': self.product.pk,
            'product_id': [self.product.pk], 'quantity_requested': ['500'],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockTransfer.objects.count(), 0)
        # Typed quantity and alert product survive the re-render.
        self.assertEqual(response.context['request_rows'][0]['quantity'], '500')
        self.assertIsNotNone(response.context['request_alert'])


class PurchaseOrderPrefillTests(AlertPrefillBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.supplier = User.objects.create_user(
            username='ap-supplier', password='pw', role=User.Role.SUPPLIER,
        )

    def po_url(self, **extra):
        params = {
            'branch': self.branch.pk, 'product': self.product.pk,
            'qty': 60, 'current': 40, 'reorder': 50,
        }
        params.update(extra)
        return reverse('procurement_add') + '?' + '&'.join(
            f'{k}={v}' for k, v in params.items()
        )

    def test_product_branch_and_qty_prefilled_without_creating_po(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.po_url())
        self.assertEqual(response.status_code, 200)
        alert = response.context['po_alert']
        self.assertEqual(alert['product'], self.product)
        self.assertEqual(alert['branch'], self.branch)
        self.assertEqual(alert['current_qty'], 40)
        self.assertEqual(
            response.context['po_restore_rows'],
            [{'product_id': str(self.product.pk), 'quantity': 60}],
        )
        self.assertEqual(response.context['po_form_data']['branch'], str(self.branch.pk))
        self.assertContains(response, 'Selected from Alert')
        self.assertEqual(Procurement.objects.count(), 0)

    def test_valid_preferred_supplier_is_preselected(self):
        SupplierProduct.objects.create(
            supplier=self.supplier, product=self.product, is_preferred=True,
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.po_url())
        self.assertEqual(response.context['po_alert']['preferred_supplier'], self.supplier)
        self.assertEqual(response.context['po_form_data']['supplier'], str(self.supplier.pk))

    def test_no_preferred_supplier_requires_choice_but_keeps_product(self):
        SupplierProduct.objects.create(
            supplier=self.supplier, product=self.product, is_preferred=False,
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.po_url())
        self.assertIsNone(response.context['po_alert']['preferred_supplier'])
        self.assertEqual(response.context['po_form_data']['supplier'], '')
        self.assertContains(response, 'Choose a supplier')
        self.assertEqual(len(response.context['po_restore_rows']), 1)

    def test_preferred_supplier_assigned_to_other_branch_is_ignored(self):
        self.supplier.supplier_profile.branch = self.other_branch
        self.supplier.supplier_profile.save(update_fields=['branch'])
        SupplierProduct.objects.create(
            supplier=self.supplier, product=self.product, is_preferred=True,
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.po_url())
        self.assertIsNone(response.context['po_alert']['preferred_supplier'])

    def test_manager_cannot_prefill_another_branch(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.po_url(branch=self.other_branch.pk))
        self.assertNotIn('po_alert', response.context)

    def test_manual_open_has_no_alert(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_add'))
        self.assertNotIn('po_alert', response.context)
        self.assertContains(response, 'Create Purchase Order')

    def test_submit_keeps_alert_on_validation_error(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('procurement_add'), {
            'branch': self.branch.pk, 'supplier': '', 'order_date': '2026-10-10',
            'funding_source': 'Cash on Hand',
            'alert_product': self.product.pk, 'alert_branch': self.branch.pk,
            'alert_qty': 60,
            'product_id': [self.product.pk], 'quantity_ordered': ['60'],
        })
        self.assertEqual(Procurement.objects.count(), 0)
        self.assertEqual(response.context['po_alert']['product'], self.product)
        self.assertEqual(
            response.context['po_restore_rows'],
            [{'product_id': str(self.product.pk), 'quantity': '60'}],
        )
