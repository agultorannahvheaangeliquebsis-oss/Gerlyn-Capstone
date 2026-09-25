"""P16 — the payment reference number did not save or persist reliably.

The reference number is the only handle anyone has for matching a GCash or
bank transfer to an order (no gateway is integrated), and it reached the
database through three doors — the Save Payment form (billing_create), the
API (PATCH /api/billing/<pk>/pay/) and online checkout — none of which
checked it beyond "is it blank":

  * Length. The column is VARCHAR(100). Nothing enforced that, so a longer
    value either crashed the request with a DataError (strict MySQL) or was
    silently truncated to a *different* reference (non-strict) — in both
    cases the number the cashier typed is not the number on the record.
  * Content. Only .strip() was applied: control characters, markup and runs
    of internal whitespace were stored verbatim, so "GC-1  2" and "GC-1 2"
    (the same transfer) could not be matched by search.
  * Lost update. billing_create and the pay API decided "still Pending" from
    a read taken before the save, then wrote every column of that stale copy.
    A confirmation landing in between was silently rolled back to Pending
    while the reference number was written over the verified record.

One rule now (billing.validators.clean_reference_number) is applied by all
three doors, and the write goes through BillingRecord.record_payment_details,
which locks the row and re-checks the status inside one transaction.
"""
import io
from decimal import Decimal

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from billing.models import BillingRecord
from billing.serializers import BillingPaymentSerializer
from branches.models import Branch
from orders.tests_checkout_and_cart_messages import StorefrontShopperTestCase
from sales.models import SalesTransaction


def _proof(name='receipt.png'):
    """A genuinely valid PNG — the upload check opens it with Pillow."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class _PaymentReferenceBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pr-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='PR Branch', location='Baliuag', owner=cls.owner
        )

    def _txn(self):
        return SalesTransaction.objects.create(
            branch=self.branch, total_amount=Decimal('250.00'),
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )


class BillingCreateReferenceTests(_PaymentReferenceBase):
    """The Save Payment form (billing_create)."""

    def _save(self, txn, reference, method='GCash', proof=True):
        client = Client()
        client.force_login(self.owner)
        data = {'payment_method': method, 'reference_number': reference}
        if proof:
            data['proof_of_payment'] = _proof()
        return client.post(f'/billing/create/{txn.pk}/', data)

    def test_a_reference_at_the_column_limit_is_stored_exactly(self):
        txn = self._txn()
        reference = 'A' * 100

        response = self._save(txn, reference)

        record = txn.billing_record
        self.assertRedirects(response, f'/billing/{record.pk}/')
        record.refresh_from_db()
        self.assertEqual(record.reference_number, reference)

    def test_an_overlong_reference_is_refused_not_crashed_or_truncated(self):
        txn = self._txn()

        response = self._save(txn, 'A' * 101)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        self.assertTrue(any('100 characters' in m for m in _messages(response)))
        record = txn.billing_record
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '')
        self.assertFalse(record.proof_of_payment)

    def test_markup_and_control_characters_are_refused(self):
        for bad in ('<script>alert(1)</script>', 'GC-1\x00', 'GC-1;DROP', 'GC-1"'):
            with self.subTest(reference=bad):
                txn = self._txn()
                response = self._save(txn, bad)

                self.assertEqual(response.status_code, 200)
                self.assertTrue(any('letters, numbers' in m for m in _messages(response)))
                record = txn.billing_record
                record.refresh_from_db()
                self.assertEqual(record.reference_number, '')

    def test_whitespace_is_normalised_before_it_is_stored(self):
        txn = self._txn()

        self._save(txn, '  GC-2026 \t  0001\n ')

        record = txn.billing_record
        record.refresh_from_db()
        self.assertEqual(record.reference_number, 'GC-2026 0001')

    def test_a_normalised_reference_is_found_by_the_billing_search(self):
        txn = self._txn()
        self._save(txn, 'GC-2026   0001')

        client = Client()
        client.force_login(self.owner)
        response = client.get('/billing/', {'q': 'GC-2026 0001'})

        self.assertContains(response, txn.order_number)


class BillingPaymentAPIReferenceTests(_PaymentReferenceBase):
    """PATCH /api/billing/<pk>/pay/."""

    def _client(self):
        client = APIClient()
        client.force_login(self.owner)
        return client

    def _pay(self, record, **data):
        payload = {'paymentMethod': 'GCash', 'proofOfPayment': _proof()}
        payload.update(data)
        return self._client().patch(
            f'/api/billing/{record.pk}/pay/', payload, format='multipart'
        )

    def test_a_valid_reference_is_persisted_and_returned(self):
        record = self._txn().billing_record

        response = self._pay(record, referenceNumber='  GC-2026   0001 ')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['referenceNumber'], 'GC-2026 0001')
        record.refresh_from_db()
        self.assertEqual(record.reference_number, 'GC-2026 0001')

    def test_an_overlong_reference_is_a_400_not_a_500_or_a_truncation(self):
        record = self._txn().billing_record

        response = self._pay(record, referenceNumber='B' * 101)

        self.assertEqual(response.status_code, 400)
        self.assertIn('referenceNumber', response.json())
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '')

    def test_markup_is_a_400(self):
        record = self._txn().billing_record

        response = self._pay(record, referenceNumber='<b>GC-1</b>')

        self.assertEqual(response.status_code, 400)
        self.assertIn('referenceNumber', response.json())
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '')

    def test_a_reference_typed_for_a_method_that_has_none_is_cleared(self):
        record = self._txn().billing_record

        response = self._pay(
            record, paymentMethod='Card', referenceNumber='typed-and-dropped',
        )

        self.assertEqual(response.status_code, 200, response.content)
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '')

    def test_a_stale_pay_cannot_roll_back_a_confirmation(self):
        """The lost update: the serializer holds a Pending copy, the record
        is confirmed underneath it, then the save lands. It must be refused —
        not write every column of the stale copy over the Paid record."""
        record = self._txn().billing_record
        record.reference_number = 'GC-ORIGINAL'
        record.proof_of_payment.save('first.png', _proof(), save=False)
        record.save()
        stale = BillingRecord.objects.get(pk=record.pk)  # Pending, as read
        BillingRecord.objects.filter(pk=record.pk).update(
            status=BillingRecord.PaymentStatus.PAID, confirmed_by=self.owner,
        )

        serializer = BillingPaymentSerializer(
            stale, data={'paymentMethod': 'GCash', 'referenceNumber': 'GC-CHANGED'},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(ValidationError):
            serializer.save()

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.owner)
        self.assertEqual(record.reference_number, 'GC-ORIGINAL')


class CheckoutReferenceTests(StorefrontShopperTestCase):
    """Online checkout — the shopper's GCash reference."""

    def _check_out(self, reference):
        return self.client.post(reverse('orders:checkout'), {
            'fulfillment_type': 'Pickup',
            'checkout_token': 'test-checkout-token',
            'mode_of_payment': 'GCash',
            'reference_number': reference,
            'proof_of_payment': _proof(),
        }, follow=True)

    def test_an_overlong_reference_is_refused_and_no_order_is_placed(self):
        response = self._check_out('C' * 101)

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertTrue(any('100 characters' in m for m in _messages(response)))
        # One field to fix, not a restart.
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 2})

    def test_markup_is_refused_and_no_order_is_placed(self):
        response = self._check_out('<script>x</script>')

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertTrue(any('letters, numbers' in m for m in _messages(response)))

    def test_a_valid_reference_is_stored_normalised(self):
        self._check_out(' GC-2026  0001 ')

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.billing_record.reference_number, 'GC-2026 0001')
