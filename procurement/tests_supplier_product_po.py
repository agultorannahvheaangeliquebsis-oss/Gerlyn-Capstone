"""Supplier-catalog restrictions on purchase-order creation."""

import datetime as dt
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from branches.models import Branch
from inventory.models import Product
from procurement.models import Procurement, SupplierProduct


class SupplierProductPOBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='catalog-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Catalog Branch', location='Baliuag', owner=cls.owner
        )
        cls.supplier = User.objects.create_user(
            username='catalog-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.other_supplier = User.objects.create_user(
            username='other-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.inactive_supplier = User.objects.create_user(
            username='inactive-supplier', password='pw',
            role=User.Role.SUPPLIER, is_active=False,
        )
        cls.assigned = Product.objects.create(
            sku_code='CAT-ASSIGNED', product_name='Assigned Rice', unit_price=80
        )
        cls.other_assigned = Product.objects.create(
            sku_code='CAT-OTHER', product_name='Other Supplier Oil', unit_price=90
        )
        cls.unassigned = Product.objects.create(
            sku_code='CAT-NONE', product_name='Unassigned Sugar', unit_price=70
        )
        cls.inactive_supplier_product = Product.objects.create(
            sku_code='CAT-INACTIVE', product_name='Inactive Supplier Item',
            unit_price=60,
        )
        SupplierProduct.objects.create(
            supplier=cls.supplier,
            product=cls.assigned,
            default_cost_price=Decimal('42.50'),
        )
        SupplierProduct.objects.create(
            supplier=cls.other_supplier,
            product=cls.other_assigned,
        )
        SupplierProduct.objects.create(
            supplier=cls.inactive_supplier,
            product=cls.inactive_supplier_product,
        )

    @classmethod
    def html_payload(cls, product):
        return {
            'branch': str(cls.branch.pk),
            'supplier': str(cls.supplier.pk),
            'order_date': dt.date(2026, 9, 20).isoformat(),
            'product_id': [str(product.pk)],
            'quantity_ordered': ['2'],
            'cost_price': ['42.50'],
        }

    @classmethod
    def api_payload(cls, product, supplier=None):
        return {
            'branchId': cls.branch.pk,
            'supplierId': (supplier or cls.supplier).pk,
            'orderDate': '2026-09-20',
            'items': [{
                'productId': product.pk,
                'quantityOrdered': 2,
                'costPrice': '42.50',
            }],
        }


class SupplierProductPOTemplateTests(SupplierProductPOBase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(self.owner)

    def test_modal_catalog_keeps_supplier_product_pairs_and_cost(self):
        response = self.client.get(reverse('procurement_list'))

        options = response.context['supplier_product_options']
        self.assertIn({
            'supplier_id': self.supplier.pk,
            'product_id': self.assigned.pk,
            'name': self.assigned.product_name,
            'sku': self.assigned.sku_code,
            'cost': '42.50',
        }, options)
        self.assertNotIn(
            self.inactive_supplier.pk,
            {item['supplier_id'] for item in options},
        )

    def test_assigned_product_can_be_ordered(self):
        response = self.client.post(
            reverse('procurement_add'), self.html_payload(self.assigned)
        )

        self.assertEqual(response.status_code, 302)
        order = Procurement.objects.get()
        self.assertEqual(order.supplier, self.supplier)
        self.assertEqual(order.items.get().product, self.assigned)

    def test_unassigned_product_post_is_rejected_without_partial_order(self):
        response = self.client.post(
            reverse('procurement_add'), self.html_payload(self.unassigned)
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'assigned to the selected supplier')
        self.assertFalse(Procurement.objects.exists())

    def test_product_assigned_to_another_supplier_is_rejected(self):
        response = self.client.post(
            reverse('procurement_add'), self.html_payload(self.other_assigned)
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'assigned to the selected supplier')
        self.assertFalse(Procurement.objects.exists())

    def test_inactive_supplier_is_not_orderable_even_with_a_mapping(self):
        payload = self.html_payload(self.inactive_supplier_product)
        payload['supplier'] = str(self.inactive_supplier.pk)

        response = self.client.post(reverse('procurement_add'), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose an active supplier')
        self.assertFalse(Procurement.objects.exists())


class SupplierProductPOAPITests(SupplierProductPOBase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_supplier_product_feed_only_returns_that_suppliers_catalog(self):
        response = self.client.get(reverse(
            'api-procurement-supplier-products', args=[self.supplier.pk]
        ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.data], [self.assigned.pk])
        self.assertEqual(response.data[0]['defaultCost'], Decimal('42.50'))

    def test_api_accepts_assigned_product(self):
        response = self.client.post(
            reverse('api-procurement-list'),
            self.api_payload(self.assigned),
            format='json',
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Procurement.objects.get().items.get().product, self.assigned)

    def test_api_rejects_unassigned_product_without_partial_order(self):
        response = self.client.post(
            reverse('api-procurement-list'),
            self.api_payload(self.unassigned),
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('assigned to the selected supplier', str(response.data))
        self.assertFalse(Procurement.objects.exists())

    def test_api_rejects_inactive_supplier_and_hides_its_catalog(self):
        create_response = self.client.post(
            reverse('api-procurement-list'),
            self.api_payload(
                self.inactive_supplier_product,
                supplier=self.inactive_supplier,
            ),
            format='json',
        )
        catalog_response = self.client.get(reverse(
            'api-procurement-supplier-products',
            args=[self.inactive_supplier.pk],
        ))

        self.assertEqual(create_response.status_code, 400)
        self.assertEqual(catalog_response.status_code, 404)
        self.assertFalse(Procurement.objects.exists())
