"""A walk-in paid by GCash or Card carries its payment evidence.

No payment gateway is integrated, so for a settlement that did not cross the
counter the reference number and the screenshot ARE the record that it
happened. Billing has always demanded them — billing_confirm refuses to mark
such a record Paid without them — but the Walk-in Sale page had no field for
either, so every GCash sale was rung up incomplete and someone had to walk it
back through Billing's "Record payment" form before it could be verified.

These pin down that the counter now captures both, that the rule it applies is
BillingRecord.missing_payment_evidence (the same one Billing, the API and
online checkout read, so the two cannot drift), and that a method with nothing
to attach is not asked for anything.
"""
import io

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts.models import StaffProfile, User
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


def _proof(name='proof.png'):
    """A genuinely valid PNG — the upload check opens it with Pillow."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class PosPaymentEvidenceTests(TestCase):
    # A valid GCash reference: exactly 13 digits (billing.validators).
    REFERENCE = '1234567890123'

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        # The cashier is Staff, so the sale is rung by one hand and verified by
        # another (A-1) — the state this evidence exists to be checked in.
        self.staff = User.objects.create_user(
            username='rey', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=self.staff, defaults={'staff_name': 'Rey', 'branch': self.branch}
        )
        self.client.force_login(self.staff)

    def _post(self, method, **extra):
        payload = {
            'branch': self.branch.pk, 'mode_of_payment': method,
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
        }
        payload.update(extra)
        return self.client.post(reverse('transaction_add'), payload)

    # -- the fields are on the page --------------------------------------------

    def test_the_form_offers_a_reference_and_a_proof_field(self):
        page = self.client.get(reverse('transaction_add'))

        self.assertContains(page, 'name="reference_number"')
        self.assertContains(page, 'name="proof_of_payment"')
        # A file input is worthless unless the form actually posts multipart.
        self.assertContains(page, 'enctype="multipart/form-data"')

    def test_the_page_carries_the_models_own_lists_for_the_show_hide(self):
        """The template decides what is VISIBLE from BillingRecord's sets, not
        from a second copy of the rule written into the JS."""
        page = self.client.get(reverse('transaction_add'))

        self.assertContains(page, 'id="pos-reference-required-methods"')
        self.assertContains(page, 'id="pos-proof-required-methods"')
        self.assertEqual(
            page.context['pos_reference_required_methods'], ['GCash']
        )
        self.assertEqual(
            sorted(page.context['pos_proof_required_methods']), ['Card', 'GCash']
        )

    # -- the happy path --------------------------------------------------------

    def test_a_gcash_walk_in_records_its_reference_and_proof(self):
        response = self._post(
            'GCash', reference_number=self.REFERENCE, proof_of_payment=_proof()
        )

        txn = SalesTransaction.objects.get()
        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        billing = txn.billing_record
        self.assertEqual(billing.reference_number, self.REFERENCE)
        self.assertTrue(billing.proof_of_payment)
        # Recording is still not settling — A-1 keeps verification separate.
        self.assertEqual(billing.status, 'Pending')
        self.assertIsNone(billing.confirmed_by)

    def test_a_card_walk_in_needs_only_the_slip(self):
        """A card payment has no reference the counter can key in, so
        PROOF_REQUIRED_METHODS covers it and REFERENCE_REQUIRED_METHODS does
        not — the sale goes through on the screenshot alone."""
        response = self._post('Card', proof_of_payment=_proof())

        txn = SalesTransaction.objects.get()
        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertTrue(txn.billing_record.proof_of_payment)
        self.assertEqual(txn.billing_record.reference_number, '')

    def test_the_evidence_is_enough_to_verify_the_payment_without_a_second_trip(self):
        """The whole point: billing_confirm used to refuse a POS GCash sale for
        missing evidence. With it captured at the counter, a second person can
        mark it Paid straight away."""
        self._post('GCash', reference_number=self.REFERENCE, proof_of_payment=_proof())
        record = SalesTransaction.objects.get().billing_record

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('billing_confirm', args=[record.pk]),
            {'status': BillingRecord.PaymentStatus.PAID},
        )

        record.refresh_from_db()
        self.assertEqual(record.status, 'Paid')
        self.assertEqual(record.confirmed_by, self.owner)
        self.assertFalse(
            any('needs its' in m for m in _messages(response)),
            'the payment should no longer be short of evidence',
        )

    # -- what is refused -------------------------------------------------------

    def test_a_gcash_walk_in_without_evidence_is_refused_with_the_basket_intact(self):
        response = self._post('GCash')

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'sales/transaction_form.html')
        self.assertIn(
            'A GCash sale needs its reference number and proof of payment '
            'before it can be recorded — without it the payment cannot be '
            'matched to this sale or verified.',
            response.context['pos_errors'],
        )
        self.assertFalse(SalesTransaction.objects.exists())
        # Rejected, not thrown away — the cashier does not re-ring the basket.
        self.assertEqual(
            response.context['pos_restore_rows'],
            [{'product_id': str(self.product.pk), 'quantity': '2'}],
        )

    def test_a_card_walk_in_without_the_slip_is_refused(self):
        response = self._post('Card')

        self.assertIn(
            'A Card sale needs its proof of payment before it can be recorded '
            '— without it the payment cannot be matched to this sale or '
            'verified.',
            response.context['pos_errors'],
        )
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_malformed_gcash_reference_is_refused_by_the_shared_rule(self):
        """billing.validators.clean_gcash_reference, not a second rule written
        into this view — 13 digits, no letters."""
        response = self._post(
            'GCash', reference_number='GC-2026-0001', proof_of_payment=_proof()
        )

        self.assertIn(
            'A GCash reference number is digits only — no letters or symbols.',
            response.context['pos_errors'],
        )
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_short_gcash_reference_is_refused(self):
        response = self._post(
            'GCash', reference_number='12345', proof_of_payment=_proof()
        )

        self.assertIn(
            'A GCash reference number is exactly 13 digits '
            '(for example 1234 567 890123).',
            response.context['pos_errors'],
        )
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_bad_reference_is_reported_once_not_also_as_missing(self):
        """The format check clears the value, so missing_payment_evidence would
        otherwise call the same field absent as well — two unrelated-looking
        complaints about one input."""
        response = self._post(
            'GCash', reference_number='nope', proof_of_payment=_proof()
        )

        errors = response.context['pos_errors']
        self.assertEqual(
            [e for e in errors if 'reference number' in e.lower()],
            ['A GCash reference number is digits only — no letters or symbols.'],
        )

    def test_a_rejected_submit_says_the_proof_has_to_be_attached_again(self):
        """A browser will not let a page pre-fill a file input, so a proof that
        was attached is gone once the form comes back. Left unsaid, fixing the
        reference number would fail a second time on a proof the cashier
        believes is still there."""
        response = self._post(
            'GCash', reference_number='nope', proof_of_payment=_proof()
        )

        self.assertTrue(response.context['pos_proof_was_posted'])
        self.assertContains(response, 'Attach the proof again')

    def test_a_first_visit_does_not_nag_about_re_attaching(self):
        page = self.client.get(reverse('transaction_add'))

        self.assertNotContains(page, 'Attach the proof again')

    def test_a_file_that_is_not_really_an_image_is_refused(self):
        """The model field is an ImageField, but assigning an upload straight
        onto it skips Django's form-layer check — a renamed .html would save
        and then serve with its own content type."""
        fake = SimpleUploadedFile(
            'proof.png', b'<html>not an image</html>', content_type='image/png'
        )
        response = self._post(
            'GCash', reference_number=self.REFERENCE, proof_of_payment=fake
        )

        self.assertIn(
            'The uploaded file is not a valid image.',
            response.context['pos_errors'],
        )
        self.assertFalse(SalesTransaction.objects.exists())

    # -- a method with nothing to attach ---------------------------------------

    def test_a_cash_walk_in_is_asked_for_neither(self):
        response = self._post('Cash', amount_tendered='1000')

        txn = SalesTransaction.objects.get()
        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(txn.billing_record.status, 'Paid')

    def test_a_reference_typed_before_switching_to_cash_is_not_stored(self):
        """Cash has no reference by definition. A value left in the field from
        an earlier method must not ride along onto the record — the same rule,
        in the same order, as billing_create."""
        self._post(
            'Cash', amount_tendered='1000',
            reference_number=self.REFERENCE, proof_of_payment=_proof(),
        )

        billing = SalesTransaction.objects.get().billing_record
        self.assertEqual(billing.reference_number, '')
        self.assertFalse(billing.proof_of_payment)

    def test_the_pending_message_says_only_verification_is_left(self):
        """The payment is recorded at the counter now, so what is outstanding
        is the separate check, not recording it at all."""
        response = self._post(
            'GCash', reference_number=self.REFERENCE, proof_of_payment=_proof()
        )

        self.assertTrue(any(
            'stays Pending until the GCash payment is verified' in m
            for m in _messages(response)
        ))
