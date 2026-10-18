"""Billing Records list — the Payment Method dropdown filters on the bill's own
payment_method (the Method column) and rides along with the status tabs and
the date-range control like the channel/fulfilment dropdowns do."""
from django.test import TestCase

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from sales.models import SalesTransaction

M = BillingRecord.PaymentMethod


class BillingPaymentMethodFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='bpm-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='BPM Branch', location='Baliuag', owner=cls.owner
        )
        cls.cash = cls._record(M.CASH)
        cls.gcash = cls._record(M.GCASH)
        cls.bank = cls._record(M.BANK_TRANSFER)

    @classmethod
    def _record(cls, method):
        txn = SalesTransaction.objects.create(
            branch=cls.branch, staff=None, total_amount=100,
        )
        record = txn.billing_record
        BillingRecord.objects.filter(pk=record.pk).update(payment_method=method)
        return record

    def _get(self, **params):
        self.client.force_login(self.owner)
        response = self.client.get('/billing/', {'range': 'all', **params})
        self.assertEqual(response.status_code, 200)
        return response

    def _ids(self, response):
        return {r.pk for r in response.context['records']}

    def test_filters_to_one_method(self):
        response = self._get(payment='GCash')
        self.assertEqual(self._ids(response), {self.gcash.pk})
        self.assertEqual(response.context['counts']['all'], 1)

    def test_method_with_a_space_in_it(self):
        response = self._get(payment='Bank Transfer')
        self.assertEqual(self._ids(response), {self.bank.pk})

    def test_no_payment_param_lists_every_method(self):
        response = self._get()
        self.assertEqual(
            self._ids(response), {self.cash.pk, self.gcash.pk, self.bank.pk}
        )

    def test_unknown_method_is_ignored_not_empty(self):
        response = self._get(payment='Bitcoin')
        self.assertEqual(response.context['payment_filter'], '')
        self.assertEqual(len(self._ids(response)), 3)

    def test_dropdown_offers_every_payment_method(self):
        response = self._get()
        for value, label in M.choices:
            self.assertContains(response, f'<option value="{value}"')
            self.assertContains(response, f'>{label}</option>')

    def test_selected_method_stays_selected(self):
        response = self._get(payment='Cash')
        self.assertContains(response, '<option value="Cash" selected>Cash</option>')

    def test_status_tabs_and_range_keep_the_payment_filter(self):
        response = self._get(payment='GCash')
        self.assertIn('payment=GCash', response.context['filter_qs'])
        self.assertEqual(dict(response.context['daterange_carry'])['payment'], 'GCash')
