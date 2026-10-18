"""Order Console — the Payment Method dropdown filters on the order's
mode_of_payment (the Payment column) and is carried by the status tabs."""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from sales.models import SalesTransaction

P = SalesTransaction.ModeOfPayment


class OrderConsolePaymentFilterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='ocp-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='OCP Branch', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)
        self.cash = self._txn(P.CASH)
        self.gcash = self._txn(P.GCASH)
        self.cod = self._txn(P.COD)

    def _txn(self, mode):
        return SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=100, mode_of_payment=mode,
        )

    def _listed(self, **params):
        response = self.client.get(
            reverse('transaction_list'), {'range': 'all', **params}
        )
        self.assertEqual(response.status_code, 200)
        return response, list(response.context['transactions'])

    def test_filters_to_one_method(self):
        response, listed = self._listed(payment='GCash')
        self.assertEqual(listed, [self.gcash])
        self.assertEqual(response.context['counts']['all'], 1)

    def test_method_with_spaces(self):
        _, listed = self._listed(payment='Cash on Delivery')
        self.assertEqual(listed, [self.cod])

    def test_unknown_method_is_ignored_not_empty(self):
        response, listed = self._listed(payment='Bitcoin')
        self.assertEqual(response.context['payment_filter'], '')
        self.assertEqual(len(listed), 3)

    def test_selected_method_stays_selected(self):
        response, _ = self._listed(payment='Cash')
        self.assertContains(response, '<option value="Cash" selected>Cash</option>')

    def test_status_tab_preserves_the_payment_filter(self):
        response, _ = self._listed(payment='GCash')
        self.assertIn('payment=GCash', response.context['filter_qs'])
        self.assertEqual(dict(response.context['daterange_carry'])['payment'], 'GCash')
