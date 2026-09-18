from decimal import Decimal

from django.test import Client, TestCase

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from sales.models import SalesTransaction


class BillingDedicatedPagesTests(TestCase):
    """Billing Detail is a dedicated page (templates/billing/billing_detail.html,
    served at billing_detail — see billing/urls.py), progressively enhanced
    into an overlay by static/js/detail-modal.js the same way every other
    module's detail page is. These checks cover the capstone ask for that
    page: Order/Customer/Amount/Method/Reference/Date/Status all shown, a
    named View Order action, and the Pending/Rejected/Paid/Cancelled action
    set.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='ddp-owner', password='pw', role=User.Role.OWNER
        )
        cls.customer = User.objects.create_user(
            username='ddp-customer', password='pw', role=User.Role.CUSTOMER,
            first_name='Dana', last_name='Cruz',
        )
        cls.branch = Branch.objects.create(
            branch_name='DDP Branch', location='Baliuag', owner=cls.owner
        )

    def _record(self, status, customer=None, payment_method=BillingRecord.PaymentMethod.CASH):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, customer=customer, total_amount=250,
        )
        record = txn.billing_record
        record.payment_method = payment_method
        if status != BillingRecord.PaymentStatus.PENDING:
            record.status = status
        record.save()
        return record

    def _get(self, record):
        client = Client()
        client.force_login(self.owner)
        response = client.get(f'/billing/{record.pk}/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_pending_record_shows_verify_and_reject_actions(self):
        record = self._record(BillingRecord.PaymentStatus.PENDING, customer=self.customer)
        html = self._get(record)
        self.assertIn('Verify Payment', html)
        self.assertIn('Reject Payment', html)
        self.assertIn('Edit payment details', html)
        self.assertNotIn('Re-open Payment', html)

    def test_rejected_record_shows_rejected_heading_and_reopen_action(self):
        record = self._record(BillingRecord.PaymentStatus.REJECTED, customer=self.customer)
        html = self._get(record)
        self.assertIn('Payment Verification Rejected', html)
        self.assertIn('Re-open Payment', html)
        self.assertIn('Edit payment details', html)
        self.assertNotIn('Verify Payment', html)

    def test_paid_record_is_read_only(self):
        record = self._record(
            BillingRecord.PaymentStatus.PAID, customer=self.customer,
            payment_method=BillingRecord.PaymentMethod.CASH,
        )
        html = self._get(record)
        self.assertNotIn('Verify Payment', html)
        self.assertNotIn('Re-open Payment', html)
        self.assertNotIn('Edit payment details', html)
        self.assertNotIn('Upload proof', html)

    def test_cancelled_record_is_read_only(self):
        record = self._record(BillingRecord.PaymentStatus.CANCELLED, customer=self.customer)
        html = self._get(record)
        self.assertNotIn('Verify Payment', html)
        self.assertNotIn('Re-open Payment', html)
        self.assertNotIn('Edit payment details', html)
        self.assertNotIn('Upload proof', html)

    def test_related_order_customer_amount_method_reference_date_status_all_shown(self):
        record = self._record(
            BillingRecord.PaymentStatus.PENDING, customer=self.customer,
            payment_method=BillingRecord.PaymentMethod.GCASH,
        )
        record.reference_number = 'GC-TEST-001'
        record.save(update_fields=['reference_number'])
        html = self._get(record)

        self.assertIn(record.transaction.order_number, html)
        self.assertIn('Dana Cruz', html)
        self.assertIn('250.00', html)
        self.assertIn('GCash', html)
        self.assertIn('GC-TEST-001', html)
        self.assertIn('Date recorded', html)
        # Status is shown once, in the heading pill (billing_detail.html no
        # longer repeats it as a "Payment status" table row).
        self.assertIn('Pending', html)

    def test_walk_in_sale_shows_walk_in_not_a_crash(self):
        record = self._record(BillingRecord.PaymentStatus.PENDING, customer=None)
        html = self._get(record)
        self.assertIn('Walk-in', html)

    def test_view_order_action_links_to_transaction_detail(self):
        record = self._record(BillingRecord.PaymentStatus.PENDING, customer=self.customer)
        html = self._get(record)
        self.assertIn('View Order', html)
        self.assertIn(f'/sales/{record.transaction.pk}/', html)

    def test_record_payment_page_is_a_dedicated_page_not_the_detail_page(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, customer=self.customer, total_amount=99,
        )
        client = Client()
        client.force_login(self.owner)
        response = client.get(f'/billing/create/{txn.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        self.assertContains(response, 'Record Payment')
