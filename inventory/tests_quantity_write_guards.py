import json

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import (
    Batch, Inventory, Product, StockMovement, Warehouse, WarehouseStock,
)


class AggregateQuantityApiGuardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='quantity-api-owner', password='pw', role=User.Role.OWNER,
        )
        self.branch = Branch.objects.create(
            owner=self.owner, branch_name='Quantity Guard Branch', location='Main',
        )
        self.warehouse = Warehouse.objects.get(branch=self.branch)
        self.product = Product.objects.create(
            sku_code='SKU-QTY-GUARD', product_name='Guarded Product', unit_price=10,
        )
        self.inventory = Inventory.objects.create(
            branch=self.branch, product=self.product,
            quantity_on_hand=5, reorder_level=2,
        )
        self.warehouse_stock = WarehouseStock.objects.create(
            warehouse=self.warehouse, product=self.product,
            quantity_on_hand=8, reorder_level=3,
        )
        self.warehouse_batch = Batch.objects.create(
            branch=self.branch, product=self.product,
            location=Batch.Location.WAREHOUSE, quantity=8,
            received_date='2026-01-01',
        )
        self.client.force_login(self.owner)

    def test_warehouse_quantity_change_is_rejected_but_reorder_update_remains(self):
        url = reverse('api-warehouse-stock-detail', args=[self.warehouse_stock.pk])

        rejected = self.client.patch(
            url,
            data=json.dumps({'quantityOnHand': 2}),
            content_type='application/json',
        )
        self.assertEqual(rejected.status_code, 400)
        self.warehouse_stock.refresh_from_db()
        self.warehouse_batch.refresh_from_db()
        self.assertEqual(self.warehouse_stock.quantity_on_hand, 8)
        self.assertEqual(self.warehouse_batch.quantity, 8)
        self.assertFalse(StockMovement.objects.exists())

        allowed = self.client.patch(
            url,
            data=json.dumps({'quantityOnHand': 8, 'reorderLevel': 6}),
            content_type='application/json',
        )
        self.assertEqual(allowed.status_code, 200)
        self.warehouse_stock.refresh_from_db()
        self.assertEqual(self.warehouse_stock.quantity_on_hand, 8)
        self.assertEqual(self.warehouse_stock.reorder_level, 6)

    def test_aggregate_api_rows_cannot_be_created_or_deleted(self):
        warehouse_list = reverse('api-warehouse-stock-list')
        warehouse_detail = reverse(
            'api-warehouse-stock-detail', args=[self.warehouse_stock.pk],
        )
        inventory_detail = reverse('api-inventory-detail', args=[self.inventory.pk])

        create_response = self.client.post(
            warehouse_list,
            data=json.dumps({'quantityOnHand': 1, 'reorderLevel': 0}),
            content_type='application/json',
        )
        self.assertEqual(create_response.status_code, 405)
        self.assertEqual(self.client.delete(warehouse_detail).status_code, 405)
        self.assertEqual(self.client.patch(
            inventory_detail,
            data=json.dumps({'quantityOnHand': 1}),
            content_type='application/json',
        ).status_code, 405)
        self.assertEqual(self.client.delete(inventory_detail).status_code, 405)
        self.assertTrue(WarehouseStock.objects.filter(pk=self.warehouse_stock.pk).exists())
        self.assertTrue(Inventory.objects.filter(pk=self.inventory.pk).exists())

    def test_read_access_and_quantity_response_fields_are_preserved(self):
        warehouse_response = self.client.get(
            reverse('api-warehouse-stock-detail', args=[self.warehouse_stock.pk]),
        )
        inventory_response = self.client.get(
            reverse('api-inventory-detail', args=[self.inventory.pk]),
        )

        self.assertEqual(warehouse_response.status_code, 200)
        self.assertEqual(warehouse_response.json()['quantityOnHand'], 8)
        self.assertEqual(inventory_response.status_code, 200)
        self.assertEqual(inventory_response.json()['quantityOnHand'], 5)


class InventoryAdminQuantityGuardTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='quantity-admin', email='admin@example.com', password='pw',
        )
        self.admin_user.role = User.Role.OWNER
        self.admin_user.save(update_fields=['role'])
        self.branch = Branch.objects.create(
            owner=self.admin_user, branch_name='Admin Guard Branch', location='Main',
        )
        self.product = Product.objects.create(
            sku_code='SKU-ADMIN-GUARD', product_name='Admin Guard Product', unit_price=10,
        )
        self.inventory = Inventory.objects.create(
            branch=self.branch, product=self.product,
            quantity_on_hand=7, reorder_level=2,
        )
        self.client.force_login(self.admin_user)

    def test_admin_keeps_quantity_read_only_but_allows_reorder_level(self):
        url = reverse('admin:inventory_inventory_change', args=[self.inventory.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'quantity_on_hand')
        self.assertNotContains(page, 'name="quantity_on_hand"')
        self.assertContains(page, 'name="reorder_level"')

        response = self.client.post(url, {
            'quantity_on_hand': '99',
            'reorder_level': '4',
            '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 7)
        self.assertEqual(self.inventory.reorder_level, 4)

    def test_admin_cannot_add_or_delete_inventory_rows(self):
        add_url = reverse('admin:inventory_inventory_add')
        delete_url = reverse('admin:inventory_inventory_delete', args=[self.inventory.pk])

        self.assertEqual(self.client.get(add_url).status_code, 403)
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertTrue(Inventory.objects.filter(pk=self.inventory.pk).exists())
