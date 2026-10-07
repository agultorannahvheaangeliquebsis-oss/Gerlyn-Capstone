"""Input ranges on the procurement forms (core.limits): supplier costs capped
at ₱250,000, lead times at a year, PO line quantities, credit notes, and the
supplier record's own text fields."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.limits import MAX_AMOUNT, MAX_LEAD_TIME_DAYS, MAX_PRICE, MAX_QUANTITY
from procurement.models import SupplierProduct
from procurement.tests_supplier_offers import OfferBase


class SupplierOfferLimitTests(OfferBase):
    def _messages(self, response):
        return [m.message for m in response.wsgi_request._messages]

    def test_a_cost_at_the_ceiling_is_accepted(self):
        self.offer(self.rice, cost=str(MAX_PRICE))
        link = SupplierProduct.objects.get(supplier=self.supplier, product=self.rice)
        self.assertEqual(link.current_cost_price, MAX_PRICE)

    def test_a_cost_over_the_ceiling_is_refused_and_nothing_is_saved(self):
        response = self.offer(self.rice, cost='250000.01')
        self.assertFalse(SupplierProduct.objects.filter(supplier=self.supplier).exists())
        text = ' '.join(self._messages(response))
        self.assertIn('250,000.00', text)

    def test_lead_time_is_capped_at_a_year(self):
        self.offer(self.rice, lead_time_days=str(MAX_LEAD_TIME_DAYS))
        self.assertEqual(
            SupplierProduct.objects.get(supplier=self.supplier).lead_time_days, MAX_LEAD_TIME_DAYS
        )
        SupplierProduct.objects.all().delete()
        response = self.offer(self.rice, lead_time_days=str(MAX_LEAD_TIME_DAYS + 1))
        self.assertFalse(SupplierProduct.objects.filter(supplier=self.supplier).exists())
        self.assertIn('Lead time cannot be more than', ' '.join(self._messages(response)))

    def test_the_portal_form_tells_the_browser_the_same_limits(self):
        self.client.force_login(self.supplier)
        html = self.client.get(reverse('supplier_portal')).content.decode()
        self.assertIn('max="250000.00"', html)
        self.assertIn(f'max="{MAX_LEAD_TIME_DAYS}"', html)


class OfferModelLimitTests(OfferBase):
    def test_model_validators_back_the_forms_up(self):
        from django.core.exceptions import ValidationError
        link = SupplierProduct(
            supplier=self.supplier, product=self.rice,
            current_cost_price=MAX_PRICE + Decimal('0.01'),
        )
        with self.assertRaises(ValidationError) as caught:
            link.full_clean(exclude=['supplier', 'product'])
        self.assertIn('current_cost_price', caught.exception.message_dict)


class SupplierRecordFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from accounts.models import User
        cls.owner = User.objects.create_user(
            username='sup-form-owner', password='pw', role=User.Role.OWNER
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def _post(self, **overrides):
        data = {'supplier_name': 'Valid Trading', 'username': 'valid-trading'}
        data.update(overrides)
        return self.client.post(reverse('supplier_add'), data)

    def _errors(self, response):
        return response.context['add_errors']

    def test_a_valid_supplier_is_created(self):
        response = self._post(contact_number='0917 123 4567', email='ok@example.com')
        self.assertRedirects(response, reverse('supplier_list'))

    def test_lengths_email_and_phone_are_checked(self):
        cases = [
            ({'supplier_name': 'S' * 101}, 'Supplier name cannot be longer than 100'),
            ({'username': 'u' * 151}, 'Username cannot be longer than 150'),
            ({'contact_person': 'p' * 101}, 'Contact person cannot be longer than 100'),
            ({'email': 'not-an-email'}, 'valid email'),
            ({'contact_number': '12345'}, 'valid PH mobile number'),
            ({'address': 'a' * 501}, 'Address cannot be longer than 500'),
        ]
        for overrides, fragment in cases:
            with self.subTest(fragment=fragment):
                response = self._post(**overrides)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(any(fragment in e for e in self._errors(response)), self._errors(response))

    def test_name_and_username_are_required(self):
        response = self._post(supplier_name='  ', username='')
        errors = ' '.join(self._errors(response))
        self.assertIn('Supplier name is required', errors)
        self.assertIn('Username is required', errors)


class CreditAmountLimitTests(TestCase):
    def test_credit_amount_range_is_enforced_by_the_service(self):
        from procurement.services import ReturnError, settle_with_credit
        for bad in ('0', '-1', 'abc', 'NaN', str(MAX_AMOUNT + Decimal('0.01')), '1e3'):
            with self.subTest(amount=bad):
                with self.assertRaises(ReturnError):
                    # Refused before the (never reached) return is even read.
                    settle_with_credit(None, None, reference='CN-1', amount=bad)


class PurchaseOrderQuantityLimitTests(OfferBase):
    def test_the_po_form_carries_the_quantity_limit(self):
        self.link()
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_add'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'max="{MAX_QUANTITY}"')
        self.assertContains(response, 'data-label="Quantity"')
