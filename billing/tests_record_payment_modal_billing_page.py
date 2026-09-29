"""Record Payment as a dialog on the Billing record's own page.

"Edit payment details" used to take the cashier to a blank full-page form
(billing_create's own billing_form.html). It now opens the same dialog
Order Detail already draws (partials/_record_payment_modal.html), posting
back with return_to=billing so it lands on billing_detail instead of
transaction_detail — see billing_create's return_to branches. These tests
pin that this second entry point actually returns to the right page, rather
than assuming reuse of the same partial makes it automatic.
"""
import shutil
import tempfile

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from billing.models import BillingRecord
from billing.tests import _valid_png_file
from branches.models import Branch
from sales.models import SalesTransaction


class RecordPaymentModalOnBillingPageTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        storage = BillingRecord._meta.get_field('proof_of_payment').storage
        scratch = tempfile.mkdtemp(prefix='record-payment-modal-billing-')
        original = storage._location
        storage._location = scratch
        for cached in ('base_location', 'location'):
            storage.__dict__.pop(cached, None)

        def restore():
            storage._location = original
            for cached in ('base_location', 'location'):
                storage.__dict__.pop(cached, None)
            shutil.rmtree(scratch, ignore_errors=True)

        cls.addClassCleanup(restore)

    def setUp(self):
        self.owner = User.objects.create_user(
            username='rpmb-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.client.force_login(self.owner)

    def _txn(self, method=SalesTransaction.ModeOfPayment.GCASH, total=275):
        return SalesTransaction.objects.create(
            branch=self.branch, total_amount=total,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            mode_of_payment=method,
        )

    def _gcash_record(self, total=275):
        # billing.signals.auto_create_billing_record always creates the
        # record as Cash regardless of the transaction's own mode_of_payment
        # — set the method on the record itself for a GCash-shaped fixture.
        txn = self._txn(total=total)
        record = txn.billing_record
        record.payment_method = BillingRecord.PaymentMethod.GCASH
        record.save(update_fields=['payment_method'])
        return record

    def _html(self, record):
        response = self.client.get(reverse('billing_detail', args=[record.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _messages(self, response):
        return [m.message for m in response.wsgi_request._messages]

    # -- the dialog on the page --------------------------------------------------

    def test_edit_payment_details_opens_the_dialog(self):
        txn = self._txn()
        record = txn.billing_record
        html = self._html(record)
        self.assertIn('id="record-payment-modal"', html)
        self.assertIn('name="return_to" value="billing"', html)
        create_url = reverse('billing_create', args=[txn.pk])
        self.assertRegex(
            html, rf'href="{create_url}\?from=billing"\s+data-payment-open="record-payment-modal"'
        )

    def test_no_dialog_once_the_payment_is_verified(self):
        txn = self._txn(method=SalesTransaction.ModeOfPayment.CASH)
        record = txn.billing_record
        record.status = BillingRecord.PaymentStatus.PAID
        record.save(update_fields=['status'])
        self.assertNotIn('id="record-payment-modal"', self._html(record))

    def test_saving_from_billing_returns_to_billing(self):
        txn = self._txn()
        record = txn.billing_record
        create_url = reverse('billing_create', args=[txn.pk])
        response = self.client.post(create_url, {
            'payment_method': 'GCash', 'reference_number': '',
            'return_to': 'billing',
        })
        # No reference and no proof for a GCash payment — refused, and sent
        # back to Billing (not Order Detail) with the dialog reopened.
        self.assertRedirects(
            response, reverse('billing_detail', args=[record.pk]) + '?record_payment=1'
        )
        self.assertTrue(any('reference number' in m for m in self._messages(response)))

        response = self.client.post(create_url, {
            'payment_method': 'GCash', 'reference_number': '2026000000101',
            'proof_of_payment': _valid_png_file(), 'return_to': 'billing',
        })
        self.assertRedirects(response, reverse('billing_detail', args=[record.pk]))
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '2026000000101')

    # -- Verify Payment is not offered when evidence is missing ------------------

    def test_verify_is_not_offered_when_evidence_is_missing(self):
        """A bill with no evidence on file has nothing to verify YET, so the
        Next Step block asks for the details instead of offering a decision.

        This used to draw Verify Payment disabled, which said "you may not
        do this" where the truth is "this is not the step you are on" — the
        step is recording the reference/proof, and Edit payment details is
        the primary action for it. billing_confirm refuses Paid either way.
        """
        record = self._gcash_record()  # GCash, no reference/proof
        html = self._html(record)
        self.assertNotIn('Verify Payment', html)
        self.assertIn('reference number', html)
        self.assertIn('Edit payment details', html)

    def test_verify_is_enabled_for_cash(self):
        txn = self._txn(method=SalesTransaction.ModeOfPayment.CASH)
        record = txn.billing_record
        html = self._html(record)
        self.assertIn(
            '<button type="submit" name="status" value="Paid" class="btn btn-primary">Verify Payment</button>',
            html,
        )

    # -- the empty-state Proof panel ----------------------------------------------

    def test_proof_panel_asks_for_the_required_proof_and_offers_the_upload(self):
        """Required-but-missing proof says so, and hands over the action.

        The panel used to read "Waiting for payment data from recorder." on
        a page whose own primary action is recording exactly that, so the
        reader was told to wait for themselves. It now states that
        {method} requires proof and opens the same Record Payment dialog —
        no new upload path.
        """
        record = self._gcash_record()  # GCash, no proof
        html = self._html(record)
        self.assertIn('Proof of payment is required for GCash', html)
        self.assertIn('Upload proof', html)
        self.assertIn('data-payment-open="record-payment-modal"', html)

    def test_proof_panel_says_no_proof_needed_for_cash(self):
        txn = self._txn(method=SalesTransaction.ModeOfPayment.CASH)
        record = txn.billing_record
        html = self._html(record)
        self.assertIn('No proof needed', html)
