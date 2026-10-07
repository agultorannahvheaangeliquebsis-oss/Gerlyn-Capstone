"""Input ranges on the Walk-in Sale form (core.limits): cash tendered, line
quantities, discount ID lengths and the ceiling on one sale's total."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.limits import MAX_AMOUNT, MAX_QUANTITY
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class WalkInSaleLimitTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='pos-limit-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='POS Limit Branch', location='Main', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-POSL', product_name='Limit Rice', unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=500, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def _sale(self, **overrides):
        data = {
            'branch': self.branch.pk,
            'mode_of_payment': 'Cash',
            'product_id': [str(self.product.pk)],
            'quantity': ['3'],
            'amount_tendered': '300',
        }
        data.update(overrides)
        return self.client.post(reverse('transaction_add'), data)

    def _assert_refused(self, response, fragment):
        self.assertEqual(response.status_code, 200)
        errors = response.context['pos_errors']
        self.assertTrue(any(fragment in e for e in errors), errors)
        self.assertFalse(SalesTransaction.objects.exists())
        self.assertEqual(Inventory.objects.get().quantity_on_hand, 500)

    def test_a_normal_sale_still_records(self):
        self.assertEqual(self._sale().status_code, 302)
        self.assertEqual(SalesTransaction.objects.get().total_amount, Decimal('300.00'))

    def test_cash_tendered_over_the_ceiling_is_refused_with_a_clear_message(self):
        self._assert_refused(
            self._sale(amount_tendered='10000000.01'),
            'Amount tendered cannot be more than ₱10,000,000.00.',
        )

    def test_cash_tendered_junk_is_refused(self):
        for raw in ('NaN', 'Infinity', '1e3', '-300', '0', '12.345', 'abc'):
            with self.subTest(raw=raw):
                response = self._sale(amount_tendered=raw)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['pos_errors'])
                self.assertFalse(SalesTransaction.objects.exists())

    def test_a_line_quantity_over_the_limit_is_reported(self):
        self._assert_refused(
            self._sale(quantity=[str(MAX_QUANTITY + 1)]),
            f'A line quantity cannot be more than {MAX_QUANTITY:,}.',
        )

    def test_one_sale_cannot_total_more_than_the_ceiling(self):
        self.product.unit_price = Decimal('250000.00')
        self.product.save()
        # 41 x 250,000 = 10,250,000 > 10,000,000, with enough stock on the shelf.
        response = self._sale(quantity=['41'], amount_tendered='10000000')
        self._assert_refused(response, 'which is more than the ₱10,000,000.00 one sale can be')

    def test_discount_id_and_name_lengths(self):
        response = self._sale(
            discount_type='PWD', discount_id_number='1' * 51, discount_holder_name='N' * 151,
        )
        errors = response.context['pos_errors']
        self.assertTrue(any('ID number cannot be longer than 50' in e for e in errors), errors)
        self.assertTrue(any('name on the ID cannot be longer than 150' in e for e in errors), errors)

    def test_the_form_carries_the_limits_to_the_browser(self):
        html = self.client.get(reverse('transaction_add'), {'branch': self.branch.pk}).content.decode()
        self.assertIn(f'max="{MAX_QUANTITY}"', html)
        self.assertIn('data-label="Amount tendered"', html)
