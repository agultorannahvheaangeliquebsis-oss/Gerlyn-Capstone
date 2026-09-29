from decimal import Decimal

from django.test import Client, TestCase

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import Product
from sales.models import SalesItem, SalesTransaction


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
        """M9 — Re-open Payment is the ONLY action on a Rejected record.

        Editing used to be offered here too, but
        BillingRecord.record_payment_details (and billing_create before it)
        accepts a Pending record only, so that action could not do anything
        except bounce with "this billing record has already been
        processed." The workflow is Re-open -> Pending -> edit; the button
        for a step the server refuses is not drawn.
        """
        record = self._record(BillingRecord.PaymentStatus.REJECTED, customer=self.customer)
        html = self._get(record)
        self.assertIn('Payment Verification Rejected', html)
        self.assertIn('Re-open Payment', html)
        self.assertNotIn('Edit payment details', html)
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
        # M5 — date_recorded is auto_now_add, written when the ORDER is
        # created (billing.signals.auto_create_billing_record), so it is the
        # bill's creation time and not "when the payment was recorded" —
        # a moment the schema does not store at all. Labelled for what it is.
        self.assertIn('Bill created', html)
        self.assertNotIn('Date recorded', html)
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

    def test_view_order_action_is_the_canonical_order_url(self):
        """Phase 4 D1 — this used to assert the link carried ?from=billing.
        That flag belonged to the old stacked Billing -> Order overlay and
        makes Order Detail hide its Payment Status card; View Order is now
        ordinary navigation, so it opens the order as-is. (?from=billing
        itself still exists — Sales honours it, and the billing_create links
        still pass it — so the test below for that behaviour stays.)"""
        record = self._record(BillingRecord.PaymentStatus.PENDING, customer=self.customer)
        html = self._get(record)
        self.assertIn(f'href="/sales/{record.transaction.pk}/"', html)
        self.assertNotIn(f'/sales/{record.transaction.pk}/?from=billing', html)

    def test_order_opened_from_billing_lists_items_instead_of_payment_status(self):
        record = self._record(BillingRecord.PaymentStatus.PENDING, customer=self.customer)
        product = Product.objects.create(
            product_name='Billing Modal Widget', sku_code='BMW-1', unit_price=Decimal('50.00'),
        )
        SalesItem.objects.create(
            transaction=record.transaction, product=product,
            quantity_sold=2, unit_price=Decimal('50.00'), subtotal=Decimal('100.00'),
        )
        client = Client()
        client.force_login(self.owner)
        response = client.get(f'/sales/{record.transaction.pk}/?from=billing&modal=1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="od-items-block"')
        # Once, not twice: there is only ever one items table.
        self.assertContains(response, 'Billing Modal Widget', count=1)
        self.assertNotContains(response, 'Payment Status')
        self.assertNotContains(response, 'View billing record')

    def test_order_opened_on_its_own_shows_the_billing_record_inline(self):
        record = self._record(BillingRecord.PaymentStatus.PENDING, customer=self.customer)
        client = Client()
        client.force_login(self.owner)
        response = client.get(f'/sales/{record.transaction.pk}/?modal=1')
        self.assertEqual(response.status_code, 200)
        # The record is read on the order itself — there is no link out to it.
        self.assertNotContains(response, 'View billing record')
        self.assertNotContains(response, f'href="/billing/{record.pk}/"')
        self.assertContains(response, 'Payment Status')
        self.assertContains(response, f'#{record.pk}')
        self.assertContains(response, 'class="od-items-block"')

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
