"""Supplier-product links rank PO suggestions without limiting selection."""
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import SupplierCategory, User
from branches.models import Branch
from inventory.models import Product
from procurement.models import Procurement, SupplierProduct


@override_settings(STORAGES={
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
})
class FilteredPoItemsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='p21-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='P21 Branch', location='Baliuag', owner=cls.owner)
        cls.canned_cat = SupplierCategory.objects.create(name='P21 Canned')
        cls.drinks_cat = SupplierCategory.objects.create(name='P21 Drinks')
        cls.unused_cat = SupplierCategory.objects.create(name='P21 Nobody')

        cls.canned_supplier = cls._supplier('p21-canned', cls.canned_cat)
        cls.drinks_supplier = cls._supplier('p21-drinks', cls.drinks_cat)
        cls.uncategorised = cls._supplier('p21-none', None)

        cls.tuna = Product.objects.create(sku_code='P21-T', product_name='P21 Tuna', unit_price=50)
        cls.cola = Product.objects.create(sku_code='P21-C', product_name='P21 Cola', unit_price=30)
        cls.beans = Product.objects.create(sku_code='P21-B', product_name='P21 Beans', unit_price=20)
        SupplierProduct.objects.create(
            supplier=cls.canned_supplier, product=cls.tuna, current_cost_price=40)
        SupplierProduct.objects.create(
            supplier=cls.drinks_supplier, product=cls.cola, current_cost_price=25)
        # Offered but not approved: must never be orderable.
        SupplierProduct.objects.create(
            supplier=cls.canned_supplier, product=cls.beans, current_cost_price=15,
            status=SupplierProduct.Status.PROPOSED)

    @classmethod
    def _supplier(cls, username, category):
        user = User.objects.create_user(
            username=username, password='pw', role=User.Role.SUPPLIER)
        profile = user.supplier_profile
        profile.supplier_name = username
        profile.category = category
        profile.save()
        return user

    def setUp(self):
        self.client.force_login(self.owner)

    def _post_form(self, supplier, *products):
        return self.client.post(reverse('procurement_add'), {
            'branch': self.branch.pk, 'supplier': supplier.pk,
            'order_date': '2026-01-01',
            'funding_source': 'Cash on Hand',
            'product_id': [str(p.pk) for p in products],
            'quantity_ordered': ['1'] * len(products),
        })

    # ---- what the form offers ------------------------------------------------
    # Create Purchase Order is its own page (procurement_add's GET), not a
    # modal on procurement_list, so these hit that page directly.

    def test_catalog_lists_each_suppliers_approved_products_only(self):
        catalog = self.client.get(reverse('procurement_add')).context['supplier_catalog']
        self.assertEqual(set(catalog[str(self.canned_supplier.pk)]), {str(self.tuna.pk)})
        self.assertEqual(set(catalog[str(self.drinks_supplier.pk)]), {str(self.cola.pk)})
        self.assertNotIn(str(self.uncategorised.pk), catalog)

    def test_category_filter_lists_only_categories_with_suppliers(self):
        context = self.client.get(reverse('procurement_add')).context
        self.assertEqual(
            set(context['supplier_categories']), {self.canned_cat, self.drinks_cat})

    def test_supplier_options_carry_their_category_for_the_filter(self):
        response = self.client.get(reverse('procurement_add'))
        self.assertContains(
            response, f'data-category="{self.canned_cat.pk}"')
        self.assertContains(response, 'id="po-supplier-category"')

    def test_product_options_for_the_row_dropdowns_are_emitted(self):
        response = self.client.get(reverse('procurement_add'))
        self.assertContains(response, 'id="po-all-products"')
        self.assertContains(response, 'Linked to this supplier')
        self.assertContains(response, 'Other products')
        options = response.context['product_options']
        self.assertIn({'id': str(self.tuna.pk), 'name': 'P21 Tuna'}, options)

    def test_form_explains_a_supplier_with_nothing_approved(self):
        response = self.client.get(reverse('procurement_add'))
        self.assertContains(response, 'id="po-no-catalog"')
        self.assertContains(response, 'No products currently linked to this supplier')
        self.assertContains(response, 'You can still create a purchase order')
        self.assertContains(response, 'Create Purchase Order')
        self.assertContains(response, 'Manage Supplier Products')

    # ---- what the server accepts ----------------------------------------------

    def test_web_accepts_a_product_the_supplier_carries(self):
        self._post_form(self.canned_supplier, self.tuna)
        self.assertEqual(Procurement.objects.count(), 1)

    def test_web_accepts_another_suppliers_product(self):
        response = self._post_form(self.canned_supplier, self.cola)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Procurement.objects.get().items.get().product, self.cola)

    def test_web_accepts_a_mixed_linked_and_manual_order(self):
        response = self._post_form(self.canned_supplier, self.tuna, self.cola)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(Procurement.objects.get().items.values_list('product_id', flat=True)),
            {self.tuna.pk, self.cola.pk},
        )

    def test_web_accepts_a_product_without_an_approved_link(self):
        response = self._post_form(self.canned_supplier, self.beans)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Procurement.objects.get().items.get().product, self.beans)

    def _api_post(self, supplier, product):
        return self.client.post(
            '/api/procurement/',
            data=json.dumps({
                'branchId': self.branch.pk, 'supplierId': supplier.pk,
                'orderDate': '2026-01-01',
                'fundingSource': 'Cash on Hand',
                'items': [{'productId': product.pk, 'quantityOrdered': 1}],
            }),
            content_type='application/json')

    def test_api_matches_the_web_rules(self):
        self.assertEqual(self._api_post(self.canned_supplier, self.tuna).status_code, 201)
        self.assertEqual(self._api_post(self.canned_supplier, self.cola).status_code, 201)
        self.assertEqual(self._api_post(self.canned_supplier, self.beans).status_code, 201)
        self.assertEqual(Procurement.objects.count(), 3)
