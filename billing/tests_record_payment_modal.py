"""Record Payment as a dialog on the order's own page.

The form used to be a page of its own (billing_create). Order Detail now draws
it as a modal that posts to the same view with return_to=order, so the cashier
lands back on the order. These tests pin the round trip — including the
payment proof going in and coming back out — and that the view is still the
one authority: the dialog is only a way of sending it the same fields.
"""
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from billing.models import BillingRecord
from billing.tests import _valid_png_file
from branches.models import Branch
from sales.models import SalesTransaction


class RecordPaymentModalTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # BillingRecord.proof_of_payment writes through its own storage, whose
        # location is fixed when the model is imported — point it at a scratch
        # folder for these tests so uploads don't land in the real media/.
        storage = BillingRecord._meta.get_field('proof_of_payment').storage
        scratch = tempfile.mkdtemp(prefix='record-payment-modal-')
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
            username='rpm-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.txn = SalesTransaction.objects.create(
            branch=self.branch, total_amount=275,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        self.record = self.txn.billing_record
        self.client.force_login(self.owner)
        self.detail_url = reverse('transaction_detail', args=[self.txn.pk])
        self.create_url = reverse('billing_create', args=[self.txn.pk])

    def _html(self, query=''):
        response = self.client.get(self.detail_url + query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _save(self, **data):
        data.setdefault('return_to', 'order')
        return self.client.post(self.create_url, data)

    def _messages(self, response):
        return [m.message for m in response.wsgi_request._messages]

    # -- the dialog on the page --------------------------------------------------

    def test_the_order_page_carries_the_dialog_posting_back_to_billing_create(self):
        html = self._html()
        self.assertIn('id="record-payment-modal"', html)
        self.assertIn(f'action="{self.create_url}"', html)
        self.assertIn('enctype="multipart/form-data"', html)
        self.assertIn('name="return_to" value="order"', html)
        self.assertIn('name="proof_of_payment"', html)
        self.assertIn('name="reference_number"', html)
        # The trigger is still a real link to the full form, for no-JS.
        self.assertRegex(
            html, rf'href="{self.create_url}"\s+data-payment-open="record-payment-modal"'
        )

    def test_the_button_says_save_only(self):
        html = self._html()
        self.assertIn('data-payment-save>Save</button>', html)
        self.assertNotIn('Save Payment', html)

    def test_the_method_choices_follow_the_orders_channel(self):
        html = self._html()
        for method in ('GCash', 'Bank Transfer', 'Cash on Delivery', 'Pay at Store'):
            self.assertIn(f'<option value="{method}"', html)
        self.assertNotIn('<option value="Cash"', html)
        self.assertNotIn('<option value="Card"', html)

    def test_no_dialog_once_the_payment_is_verified_or_from_the_billing_record(self):
        self.assertIn('id="record-payment-modal"', self._html())
        self.assertNotIn('id="record-payment-modal"', self._html('?from=billing'))
        BillingRecord.objects.filter(pk=self.record.pk).update(
            status=BillingRecord.PaymentStatus.PAID
        )
        self.assertNotIn('id="record-payment-modal"', self._html())

    # -- saving, and the proof making the round trip ----------------------------------

    def test_saving_returns_to_the_order_and_the_proof_can_be_viewed(self):
        png = _valid_png_file()
        expected = png.read()
        png.seek(0)
        response = self._save(
            payment_method='GCash', reference_number='2026000000001', proof_of_payment=png,
        )
        self.assertRedirects(response, self.detail_url)
        self.assertIn(
            'Payment recorded. It still needs verification by another '
            'staff member or manager.',
            self._messages(response),
        )

        self.record.refresh_from_db()
        self.assertEqual(self.record.payment_method, 'GCash')
        self.assertEqual(self.record.reference_number, '2026000000001')
        self.assertTrue(self.record.proof_of_payment)

        # The order page now lists the reference and links the proof...
        html = self._html()
        self.assertIn('2026000000001', html)
        proof_url = reverse('billing_proof', args=[self.record.pk])
        self.assertIn(f'href="{proof_url}"', html)

        # ...and that link streams the very bytes that were uploaded.
        proof = self.client.get(proof_url)
        self.assertEqual(proof.status_code, 200)
        self.assertEqual(proof['Content-Type'], 'image/png')
        self.assertEqual(b''.join(proof.streaming_content), expected)

    def test_a_missing_proof_sends_the_cashier_back_with_the_dialog_reopened(self):
        response = self._save(payment_method='GCash', reference_number='1000000000001')
        self.assertRedirects(response, self.detail_url + '?record_payment=1')
        self.assertTrue(any('proof of payment' in m for m in self._messages(response)))
        self.record.refresh_from_db()
        self.assertEqual(self.record.reference_number, '')
        self.assertFalse(self.record.proof_of_payment)

    def test_a_missing_reference_is_refused_the_same_way(self):
        response = self._save(payment_method='GCash', proof_of_payment=_valid_png_file())
        self.assertRedirects(response, self.detail_url + '?record_payment=1')
        self.assertTrue(any('reference number' in m for m in self._messages(response)))
        self.record.refresh_from_db()
        self.assertFalse(self.record.proof_of_payment)

    def test_a_file_that_is_not_an_image_is_refused(self):
        fake = SimpleUploadedFile('proof.png', b'<html>not an image</html>', content_type='image/png')
        response = self._save(
            payment_method='GCash', reference_number='1000000000002', proof_of_payment=fake,
        )
        self.assertRedirects(response, self.detail_url + '?record_payment=1')
        self.assertTrue(any('not a valid image' in m for m in self._messages(response)))
        self.record.refresh_from_db()
        self.assertFalse(self.record.proof_of_payment)

    def test_a_method_the_channel_does_not_offer_is_refused(self):
        BillingRecord.objects.filter(pk=self.record.pk).update(payment_method='GCash')
        response = self._save(payment_method='Cash')
        self.assertRedirects(response, self.detail_url + '?record_payment=1')
        self.record.refresh_from_db()
        self.assertEqual(self.record.payment_method, 'GCash')

    def test_cash_on_delivery_saves_with_nothing_attached(self):
        response = self._save(payment_method='Cash on Delivery')
        self.assertRedirects(response, self.detail_url)
        self.record.refresh_from_db()
        self.assertEqual(self.record.payment_method, 'Cash on Delivery')

    def test_a_record_that_moved_on_meanwhile_is_not_overwritten(self):
        before = self.record.payment_method
        BillingRecord.objects.filter(pk=self.record.pk).update(
            status=BillingRecord.PaymentStatus.PAID
        )
        response = self._save(payment_method='Cash on Delivery')
        # billing_create turns a non-Pending record away before it reads the form.
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(self.record.payment_method, before)

    # -- the flag only chooses between two known destinations --------------------

    def test_without_the_flag_the_old_page_flow_is_unchanged(self):
        response = self.client.post(self.create_url, {'payment_method': 'Cash on Delivery'})
        self.assertRedirects(response, f'/billing/{self.record.pk}/')

    def test_an_unknown_return_to_is_ignored(self):
        response = self._save(payment_method='Cash on Delivery', return_to='https://evil.example/')
        self.assertRedirects(response, f'/billing/{self.record.pk}/')
