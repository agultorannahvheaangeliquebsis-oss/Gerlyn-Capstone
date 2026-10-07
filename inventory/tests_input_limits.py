"""Input ranges on the inventory forms: the ₱250,000 price ceiling and the
quantity / length limits that sit beside it (core.limits)."""
import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.limits import MAX_PRICE, MAX_QUANTITY
from inventory.models import Inventory, Product, ProductPriceTier, Warehouse, WarehouseStock


class ProductPriceCeilingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='limit-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def _add(self, **overrides):
        data = {
            'product_name': 'Ceiling Test Product',
            'unit_of_measure': Product.UnitOfMeasure.PIECE,
            'unit_price': '10.00',
        }
        data.update(overrides)
        return self.client.post(reverse('product_add'), data)

    def _assert_refused(self, response, fragment):
        self.assertEqual(response.status_code, 200)
        errors = response.context['add_errors']
        self.assertTrue(any(fragment in e for e in errors), errors)
        self.assertFalse(Product.objects.filter(product_name='Ceiling Test Product').exists())

    # -- the headline rule ----------------------------------------------------

    def test_a_retail_price_of_exactly_250000_is_accepted(self):
        response = self._add(unit_price='250000')
        self.assertRedirects(response, reverse('product_list'))
        self.assertEqual(
            Product.objects.get(product_name='Ceiling Test Product').unit_price,
            Decimal('250000.00'),
        )

    def test_a_retail_price_above_250000_is_rejected_with_a_clear_message(self):
        self._assert_refused(
            self._add(unit_price='250000.01'),
            'Retail price cannot be more than ₱250,000.00.',
        )

    def test_a_ridiculous_price_is_rejected_not_a_database_error(self):
        self._assert_refused(self._add(unit_price='99999999999'), 'cannot be more than')

    def test_non_finite_and_malformed_prices_are_rejected(self):
        for raw in ('NaN', 'Infinity', '1e5', '12.345', ''):
            with self.subTest(raw=raw):
                response = self._add(unit_price=raw)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['add_errors'])
                self.assertFalse(
                    Product.objects.filter(product_name='Ceiling Test Product').exists()
                )

    def test_the_form_tells_the_browser_the_same_limit(self):
        html = self.client.get(reverse('product_add')).content.decode()
        self.assertIn('max="250000.00"', html)
        self.assertIn('min="0.01"', html)
        self.assertIn('maxlength="150"', html)

    # -- bulk prices ------------------------------------------------------------

    def test_a_bulk_price_above_the_ceiling_is_rejected(self):
        # Retail is also at the ceiling so the only thing wrong is the tier.
        response = self._add(
            unit_price='250000', tier_min_quantity=['10'], tier_price=['250000.01'],
        )
        self._assert_refused(response, 'Bulk price 1: the price cannot be more than ₱250,000.00.')

    def test_a_bulk_quantity_above_the_limit_is_rejected(self):
        response = self._add(
            unit_price='50', tier_min_quantity=[str(MAX_QUANTITY + 1)], tier_price=['40'],
        )
        self._assert_refused(response, f'{MAX_QUANTITY:,}')

    def test_a_valid_bulk_ladder_still_saves(self):
        response = self._add(unit_price='50', tier_min_quantity=['10'], tier_price=['45.50'])
        self.assertRedirects(response, reverse('product_list'))
        tier = ProductPriceTier.objects.get(product__product_name='Ceiling Test Product')
        self.assertEqual((tier.min_quantity, tier.unit_price), (10, Decimal('45.50')))

    # -- the other fields -------------------------------------------------------

    def test_name_barcode_and_description_lengths(self):
        self._assert_refused(self._add(product_name='N' * 151), 'Product name cannot be longer than 150')
        # a name that is too long is not "Ceiling Test Product", so check by count
        self.assertEqual(Product.objects.count(), 0)
        response = self._add(barcode='9' * 65)
        self._assert_refused(response, 'Barcode cannot be longer than 64')
        response = self._add(description='d' * 1001)
        self._assert_refused(response, 'Description cannot be longer than 1000')

    def test_product_name_is_required(self):
        response = self._add(product_name='   ')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('Product name is required' in e for e in response.context['add_errors']))

    def test_unit_of_measure_must_be_one_of_the_choices(self):
        self._assert_refused(self._add(unit_of_measure='Truckload'), 'unit of measure')

    def test_category_must_exist(self):
        self._assert_refused(self._add(category='99999'), 'category')
        self._assert_refused(self._add(category='abc'), 'category')

    def test_edit_enforces_the_same_ceiling(self):
        product = Product.objects.create(sku_code='SKU-EDIT', product_name='Editable', unit_price=10)
        response = self.client.post(reverse('product_edit', args=[product.pk]), {
            'product_name': 'Editable', 'unit_of_measure': Product.UnitOfMeasure.PIECE,
            'unit_price': '250001',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('cannot be more than' in e for e in response.context['edit_errors']))
        product.refresh_from_db()
        self.assertEqual(product.unit_price, Decimal('10.00'))


class ModelAndApiCeilingTests(TestCase):
    """The same ceiling holds below the forms: model validation and the API."""

    def test_model_validation_refuses_a_price_over_the_ceiling(self):
        product = Product(sku_code='SKU-M', product_name='M', unit_price=MAX_PRICE + Decimal('0.01'))
        with self.assertRaises(ValidationError) as caught:
            product.full_clean(exclude=['category'])
        self.assertIn('unit_price', caught.exception.message_dict)

    def test_model_validation_accepts_the_ceiling_itself(self):
        Product(sku_code='SKU-M2', product_name='M2', unit_price=MAX_PRICE).full_clean(exclude=['category'])

    def test_api_refuses_a_retail_price_over_the_ceiling(self):
        owner = User.objects.create_user(username='api-limit', password='pw', role=User.Role.OWNER)
        self.client.force_login(owner)
        product = Product.objects.create(sku_code='SKU-API', product_name='Api', unit_price=10)
        response = self.client.patch(
            reverse('api-product-detail', args=[product.pk]),
            data=json.dumps({'retailPrice': '250000.01'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        product.refresh_from_db()
        self.assertEqual(product.unit_price, Decimal('10.00'))


class StockQuantityLimitTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='qty-limit-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(owner=self.owner, branch_name='Limit Branch', location='Main')
        self.warehouse = Warehouse.objects.get(branch=self.branch)
        self.product = Product.objects.create(sku_code='SKU-QL', product_name='QL', unit_price=10)
        self.inventory = Inventory.objects.create(
            branch=self.branch, product=self.product, quantity_on_hand=5, reorder_level=2,
        )
        self.warehouse_stock = WarehouseStock.objects.create(
            warehouse=self.warehouse, product=self.product, quantity_on_hand=8, reorder_level=3,
        )
        self.client.force_login(self.owner)

    def _messages(self, response):
        return [m.message for m in response.context['messages']] if response.context else []

    def test_reorder_level_at_the_limit_saves_and_over_it_does_not(self):
        url = reverse('inventory_edit', args=[self.inventory.pk])
        self.client.post(url, {'reorder_level': str(MAX_QUANTITY)})
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.reorder_level, MAX_QUANTITY)

        response = self.client.post(url, {'reorder_level': str(MAX_QUANTITY + 1)}, follow=True)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.reorder_level, MAX_QUANTITY)
        self.assertTrue(any('cannot be more than' in m for m in self._messages(response)))

    def test_reorder_level_rejects_junk_and_a_giant_digit_string(self):
        url = reverse('inventory_edit', args=[self.inventory.pk])
        for raw in ('abc', '-1', '1.5', '9' * 5000):
            with self.subTest(raw=raw[:10]):
                response = self.client.post(url, {'reorder_level': raw}, follow=True)
                self.assertEqual(response.status_code, 200)
                self.inventory.refresh_from_db()
                self.assertEqual(self.inventory.reorder_level, 2)

    def test_warehouse_reorder_level_is_bounded_too(self):
        url = reverse('warehouse_stock_edit', args=[self.warehouse_stock.pk])
        self.client.post(url, {'reorder_level': str(MAX_QUANTITY + 1)})
        self.warehouse_stock.refresh_from_db()
        self.assertEqual(self.warehouse_stock.reorder_level, 3)

    def test_a_counted_quantity_over_the_limit_is_refused(self):
        response = self.client.post(reverse('stock_adjustment_add'), {
            'product_id': self.product.pk, 'branch_id': self.branch.pk,
            'counted_quantity': str(MAX_QUANTITY + 1), 'reason': 'Recount',
        })
        self.assertEqual(response.status_code, 200)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 5)

    def test_a_stock_adjustment_with_a_non_numeric_product_is_an_error_not_a_500(self):
        response = self.client.post(reverse('stock_adjustment_add'), {
            'product_id': 'abc', 'branch_id': 'xyz', 'counted_quantity': '3', 'reason': 'x',
        })
        self.assertEqual(response.status_code, 200)

    def test_the_stock_forms_carry_the_limit_to_the_browser(self):
        html = self.client.get(reverse('inventory_list')).content.decode()
        self.assertIn(f'max="{MAX_QUANTITY}"', html)

    def test_a_withdrawal_line_over_the_limit_is_reported_not_dropped(self):
        response = self.client.post(reverse('warehouse_stock_request'), {
            'branch_id': self.branch.pk, 'source_branch_id': self.branch.pk,
            'product_id': [str(self.product.pk)], 'quantity_requested': [str(MAX_QUANTITY + 1)],
        }, follow=True)
        text = ' '.join(self._messages(response))
        self.assertIn('cannot be more than', text)
