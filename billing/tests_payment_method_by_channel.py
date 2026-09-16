"""B-3 — billing_create always offered (and accepted) all six
BillingRecord.PaymentMethod choices, regardless of the transaction's
channel. A Physical (walk-in) sale could be "settled" as Cash on Delivery,
and an Online order as a counter Cash or Card swipe — neither of which the
counter, or the customer, could ever have actually done.

_payment_methods_for_channel() now scopes the choices to the transaction's
transaction_type, and billing_create's POST validation checks against that
same scoped set, not the full enum — so a tampered POST can't smuggle in a
channel-inappropriate method either.
"""
from django.contrib.messages import get_messages
from django.test import Client, TestCase

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from sales.models import SalesTransaction


class BillingPaymentMethodByChannelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='b3-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='B3 Branch', location='Baliuag', owner=cls.owner
        )

    def _txn(self, transaction_type):
        return SalesTransaction.objects.create(
            branch=self.branch, total_amount=250,
            transaction_type=transaction_type,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )

    def _client(self):
        client = Client()
        client.force_login(self.owner)
        return client

    def _post(self, txn, payment_method):
        return self._client().post(
            f'/billing/create/{txn.pk}/',
            {'payment_method': payment_method, 'reference_number': 'REF-1'},
        )

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_physical_sale_is_not_offered_online_only_methods(self):
        txn = self._txn(SalesTransaction.TransactionType.PHYSICAL)
        response = self._client().get(f'/billing/create/{txn.pk}/')
        offered = {value for value, _ in response.context['payment_choices']}
        self.assertEqual(offered, {'Cash', 'GCash', 'Card', 'Bank Transfer'})

    def test_online_order_is_not_offered_in_person_methods(self):
        txn = self._txn(SalesTransaction.TransactionType.ONLINE)
        response = self._client().get(f'/billing/create/{txn.pk}/')
        offered = {value for value, _ in response.context['payment_choices']}
        self.assertEqual(
            offered, {'GCash', 'Bank Transfer', 'Cash on Delivery', 'Pay at Store'}
        )

    def test_cash_on_delivery_is_rejected_for_a_physical_sale(self):
        txn = self._txn(SalesTransaction.TransactionType.PHYSICAL)
        response = self._post(txn, 'Cash on Delivery')

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        self.assertIn(
            'Choose a valid payment method to record this payment.',
            self._messages(response),
        )
        txn.billing_record.refresh_from_db()
        self.assertEqual(txn.billing_record.reference_number, '')

    def test_cash_is_rejected_for_an_online_order(self):
        txn = self._txn(SalesTransaction.TransactionType.ONLINE)
        response = self._post(txn, 'Cash')

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'Choose a valid payment method to record this payment.',
            self._messages(response),
        )
        txn.billing_record.refresh_from_db()
        self.assertEqual(txn.billing_record.reference_number, '')

    def test_cash_still_saves_for_a_physical_sale(self):
        """Regression guard — the channel scope must not disturb a normal
        in-person submit."""
        txn = self._txn(SalesTransaction.TransactionType.PHYSICAL)
        response = self._post(txn, 'Cash')

        self.assertRedirects(response, f'/billing/{txn.billing_record.pk}/')
        txn.billing_record.refresh_from_db()
        self.assertEqual(txn.billing_record.payment_method, 'Cash')

    def test_cash_on_delivery_still_saves_for_an_online_order(self):
        """Regression guard for the other channel."""
        txn = self._txn(SalesTransaction.TransactionType.ONLINE)
        response = self._post(txn, 'Cash on Delivery')

        self.assertRedirects(response, f'/billing/{txn.billing_record.pk}/')
        txn.billing_record.refresh_from_db()
        self.assertEqual(txn.billing_record.payment_method, 'Cash on Delivery')
