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
            'payment_method': 'GCash', 'reference_number': 'GC-BILLING-1',
            'proof_of_payment': _valid_png_file(), 'return_to': 'billing',
        })
        self.assertRedirects(response, reverse('billing_detail', args=[record.pk]))
        record.refresh_from_db()
        self.assertEqual(record.reference_number, 'GC-BILLING-1')

    # -- Verify Payment disabled when evidence is missing ------------------------

    def test_verify_is_disabled_when_evidence_is_missing(self):
        record = self._gcash_record()  # GCash, no reference/proof
        html = self._html(record)
        self.assertIn('<button type="button" class="btn btn-primary" disabled>Verify Payment</button>', html)
        self.assertIn('reference number', html)

    def test_verify_is_enabled_for_cash(self):
        txn = self._txn(method=SalesTransaction.ModeOfPayment.CASH)
        record = txn.billing_record
        html = self._html(record)
        self.assertIn(
            '<button type="submit" name="status" value="Paid" class="btn btn-primary">Verify Payment</button>',
            html,
        )

    # -- the empty-state Proof panel ----------------------------------------------

    def test_proof_panel_shows_a_passive_notice_not_an_upload_button(self):
        record = self._gcash_record()  # GCash, no proof
        html = self._html(record)
        self.assertIn('Waiting for payment data from recorder.', html)
        self.assertNotIn('Upload proof', html)

    def test_proof_panel_says_no_proof_needed_for_cash(self):
        txn = self._txn(method=SalesTransaction.ModeOfPayment.CASH)
        record = txn.billing_record
        html = self._html(record)
        self.assertIn('No proof needed', html)
