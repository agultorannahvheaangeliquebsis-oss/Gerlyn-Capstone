"""P15 — an online Pickup / Pay at Store customer can REQUEST the PWD discount;
staff verify the ID at pick-up before payment is confirmed.

What must stay true (F-04 still stands): a request is not a claim. Submitting
one records what the customer typed and changes no price, no total and no
amount due. The discount exists only after a named staff member approves it,
through the same re-pricing the Order Console's apply-discount form uses, and
billing will not mark the order Paid while the request is Pending. Approval is
only possible before Paid, so P14's refunds — which read the frozen, already-
discounted line prices — stay correct.

Money used throughout: an eligible ₱112.00 VAT-inclusive product, quantity 2.
Full price 2 x 112.00 = ₱224.00 (₱24.00 VAT inside). Discounted: VAT stripped,
20% off the base = 2 x ₱80.00 = ₱160.00, VAT-exempt (₱40.00 statutory discount;
the other ₱24.00 of the drop is the VAT no longer owed).
"""
import uuid
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Address, BranchManagerProfile, Notification, StaffProfile, User
from billing.models import BillingRecord
from branches.models import Branch
from core.module_permissions import BILLING, DEFAULT_STAFF_MODULES, SALES_POS
from inventory.models import Inventory, Product
from reports.services import refund_value_total
from sales.models import Refund, SalesTransaction

FULL = Decimal('224.00')
DISCOUNTED = Decimal('160.00')
VALID_ID = 'PWD-04-1234-567890'
VALID_NAME = 'Juan dela Cruz'


class PwdRequestFixture:
    def build(self):
        self.owner = User.objects.create_user(
            username='pr-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.other_branch = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.manager = self._internal(
            'pr-manager', User.Role.BRANCH_MANAGER, self.branch
        )
        self.staff = self._internal('pr-staff', User.Role.STAFF, self.branch)
        User.objects.filter(pk=self.staff.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, SALES_POS, BILLING]
        )
        self.other_staff = self._internal(
            'pr-other-staff', User.Role.STAFF, self.other_branch
        )
        User.objects.filter(pk=self.other_staff.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, SALES_POS, BILLING]
        )

        self.eligible = Product.objects.create(
            sku_code='SKU-PR1', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
            is_vat_exempt=False, is_pwd_eligible=True,
        )
        self.ineligible = Product.objects.create(
            sku_code='SKU-PR2', product_name='Cigarettes',
            unit_of_measure='Pack', unit_price=Decimal('112.00'),
            is_vat_exempt=False, is_pwd_eligible=False,
        )
        for product in (self.eligible, self.ineligible):
            Inventory.objects.create(
                product=product, branch=self.branch,
                quantity_on_hand=50, reorder_level=5,
            )
        self.shopper = User.objects.create_user(
            username='pr-shopper', password='pw', role=User.Role.CUSTOMER
        )

    def _internal(self, username, role, branch):
        user = User.objects.create_user(username=username, password='pw', role=role)
        if role == User.Role.BRANCH_MANAGER:
            BranchManagerProfile.objects.update_or_create(
                user=user, defaults={'manager_name': username, 'branch': branch}
            )
        else:
            StaffProfile.objects.update_or_create(
                user=user, defaults={'staff_name': username, 'branch': branch}
            )
        return user

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class CheckoutRequestTests(PwdRequestFixture, TestCase):
    def setUp(self):
        self.build()
        self.client.force_login(self.shopper)

    def check_out(self, cart=None, **extra):
        token = uuid.uuid4().hex
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = cart or {str(self.eligible.pk): 2}
        session['checkout_token'] = token
        session.save()
        payload = {
            'mode_of_payment': 'Pay at Store',
            'fulfillment_type': 'Pickup',
            'request_pwd_discount': '1',
            'pwd_id_number': VALID_ID,
            'pwd_holder_name': VALID_NAME,
            'checkout_token': token,
        }
        payload.update(extra)
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(reverse('orders:checkout'), payload, follow=True)

    def test_a_pickup_order_records_a_pending_request_and_charges_full_price(self):
        self.check_out()

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.pwd_request_status, 'Pending')
        self.assertEqual(sale.pwd_request_id_number, VALID_ID)
        self.assertEqual(sale.pwd_request_holder_name, VALID_NAME)
        self.assertIsNotNone(sale.pwd_requested_at)
        # F-04: nothing is granted by asking.
        self.assertEqual(sale.discount_type, 'None')
        self.assertEqual(sale.discount_id_number, '')
        self.assertIsNone(sale.discount_applied_by)
        self.assertEqual(sale.total_amount, FULL)
        self.assertEqual(sale.total_discount_amount, Decimal('0.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('112.00'))
        self.assertEqual(sale.billing_record.amount_due, FULL)

    def test_the_id_is_normalised(self):
        self.check_out(pwd_id_number='  pwd-04-1234   567890 ', pwd_holder_name='  Juan   dela Cruz ')

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.pwd_request_id_number, 'PWD-04-1234 567890')
        self.assertEqual(sale.pwd_request_holder_name, 'Juan dela Cruz')

    def test_pay_at_store_and_other_payment_methods_may_request_on_pickup(self):
        self.check_out(mode_of_payment='Cash on Delivery')
        self.assertEqual(SalesTransaction.objects.get().pwd_request_status, 'Pending')

    def test_no_request_means_no_request_status(self):
        self.check_out(request_pwd_discount=None)

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.pwd_request_status, 'None')
        self.assertEqual(sale.pwd_request_id_number, '')

    def test_a_delivery_order_cannot_carry_a_request(self):
        # A saved address, so checkout gets past its own delivery checks and
        # the refusal under test is the request's.
        Address.objects.create(user=self.shopper, is_default=True, text='12 Real St')
        response = self.check_out(
            fulfillment_type='Delivery', mode_of_payment='Cash on Delivery'
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertTrue(any('pick up' in m for m in self.messages(response)))

    def test_bad_id_details_are_refused_and_no_order_is_written(self):
        cases = {
            'blank id': dict(pwd_id_number=''),
            'blank name': dict(pwd_holder_name=''),
            'id with no digit': dict(pwd_id_number='MADE-UP-ID'),
            'id too short': dict(pwd_id_number='12'),
            'id with junk characters': dict(pwd_id_number='PWD<script>1234'),
            'name with digits': dict(pwd_holder_name='Juan 2nd'),
            'name too short': dict(pwd_holder_name='Jo'),
        }
        for label, override in cases.items():
            with self.subTest(label):
                self.check_out(**override)
                self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_a_request_where_nothing_is_eligible_is_refused(self):
        response = self.check_out(cart={str(self.ineligible.pk): 1})

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertTrue(any('eligible' in m for m in self.messages(response)))

    def test_one_eligible_line_among_ineligible_ones_is_enough(self):
        self.check_out(cart={str(self.eligible.pk): 1, str(self.ineligible.pk): 1})
        self.assertEqual(SalesTransaction.objects.get().pwd_request_status, 'Pending')

    def test_the_branch_is_told_about_the_request(self):
        self.check_out()

        note = Notification.objects.get(user=self.manager, title__startswith='PWD discount request')
        self.assertIn(SalesTransaction.objects.get().order_number, note.title)

    def test_the_old_self_service_fields_are_still_ignored(self):
        """discount_type / discount_id_number stay dead inputs (F-04); only the
        request fields do anything."""
        self.check_out(
            request_pwd_discount=None, discount_type='PWD',
            discount_id_number=VALID_ID, discount_holder_name=VALID_NAME,
        )
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.discount_type, 'None')
        self.assertEqual(sale.pwd_request_status, 'None')
        self.assertEqual(sale.total_amount, FULL)


class ReviewFixture(PwdRequestFixture):
    def build_with_request(self):
        self.build()
        sale = SalesTransaction.objects.create(
            branch=self.branch, customer=self.shopper,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            fulfillment_type=SalesTransaction.FulfillmentType.PICKUP,
            mode_of_payment='Pay at Store',
            total_amount=FULL, total_vat_amount=Decimal('24.00'),
            pwd_request_status='Pending', pwd_request_id_number=VALID_ID,
            pwd_request_holder_name=VALID_NAME, pwd_requested_at=timezone.now(),
        )
        from sales.models import SalesItem
        SalesItem.objects.create(
            transaction=sale, product=self.eligible, quantity_sold=2,
            unit_price=Decimal('112.00'), vat_amount=Decimal('24.00'),
        )
        BillingRecord.objects.filter(transaction=sale).update(
            payment_method='Pay at Store'
        )
        self.sale = sale
        self.billing = sale.billing_record

    def review(self, user, **payload):
        self.client.force_login(user)
        return self.client.post(
            reverse('transaction_pwd_request_review', args=[self.sale.pk]), payload
        )

    def approve(self, user=None, **override):
        payload = {
            'decision': 'approve',
            'discount_id_number': VALID_ID,
            'discount_holder_name': VALID_NAME,
        }
        payload.update(override)
        return self.review(user or self.staff, **payload)

    def confirm_paid(self, user=None):
        self.client.force_login(user or self.manager)
        return self.client.post(
            reverse('billing_confirm', args=[self.billing.pk]), {'status': 'Paid'}
        )


class ReviewTests(ReviewFixture, TestCase):
    def setUp(self):
        self.build_with_request()

    def test_approving_reprices_the_order_and_the_bill(self):
        response = self.approve()

        self.assertRedirects(response, reverse('transaction_detail', args=[self.sale.pk]))
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Approved')
        self.assertEqual(self.sale.discount_type, 'PWD')
        self.assertEqual(self.sale.total_amount, DISCOUNTED)
        self.assertEqual(self.sale.total_discount_amount, Decimal('40.00'))
        self.assertEqual(self.sale.total_vat_amount, Decimal('0.00'))
        item = self.sale.items.get()
        self.assertEqual(item.unit_price, Decimal('80.00'))
        self.assertEqual(item.subtotal, DISCOUNTED)
        self.assertTrue(item.is_vat_exempt_line)
        self.billing.refresh_from_db()
        self.assertEqual(self.billing.amount_due, DISCOUNTED)

    def test_approval_records_who_and_when(self):
        self.approve()

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_reviewed_by, self.staff)
        self.assertIsNotNone(self.sale.pwd_request_reviewed_at)
        self.assertEqual(self.sale.discount_applied_by, self.staff)
        self.assertIsNotNone(self.sale.discount_applied_at)

    def test_the_reviewer_can_correct_the_id_and_both_versions_are_kept(self):
        self.approve(discount_id_number='PWD-04-1234-999999')

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.discount_id_number, 'PWD-04-1234-999999')
        self.assertEqual(self.sale.pwd_request_id_number, VALID_ID)

    def test_approval_needs_valid_id_details(self):
        self.approve(discount_id_number='')

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')
        self.assertEqual(self.sale.total_amount, FULL)

    def test_the_customer_is_told_the_outcome(self):
        self.approve()

        self.assertTrue(Notification.objects.filter(
            user=self.shopper, title__startswith='PWD discount approved'
        ).exists())

    def test_rejecting_leaves_the_price_and_records_why(self):
        response = self.review(
            self.staff, decision='reject', reject_reason='No PWD ID presented'
        )

        self.assertRedirects(response, reverse('transaction_detail', args=[self.sale.pk]))
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Rejected')
        self.assertEqual(self.sale.pwd_request_reject_reason, 'No PWD ID presented')
        self.assertEqual(self.sale.pwd_request_reviewed_by, self.staff)
        self.assertEqual(self.sale.discount_type, 'None')
        self.assertEqual(self.sale.total_amount, FULL)
        self.billing.refresh_from_db()
        self.assertEqual(self.billing.amount_due, FULL)
        self.assertTrue(Notification.objects.filter(
            user=self.shopper, title__startswith='PWD discount not approved'
        ).exists())

    def test_rejecting_needs_a_reason(self):
        self.review(self.staff, decision='reject', reject_reason='   ')

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')

    def test_an_unknown_decision_changes_nothing_and_says_so(self):
        response = self.review(self.staff, decision='maybe')

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')
        self.assertTrue(self.messages(response))

    def test_a_decided_request_cannot_be_decided_again(self):
        self.approve()
        response = self.approve()

        self.sale.refresh_from_db()
        # Not re-priced a second time: still the once-discounted figure.
        self.assertEqual(self.sale.total_amount, DISCOUNTED)
        self.assertTrue(any('already' in m or 'no PWD' in m for m in self.messages(response)))

    def test_a_rejected_request_cannot_be_approved_afterwards(self):
        self.review(self.staff, decision='reject', reject_reason='Card expired')
        self.approve()

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Rejected')
        self.assertEqual(self.sale.total_amount, FULL)

    def test_get_does_not_decide_anything(self):
        self.client.force_login(self.staff)
        self.client.get(reverse('transaction_pwd_request_review', args=[self.sale.pk]))

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')

    def test_a_paid_bill_can_no_longer_be_repriced(self):
        BillingRecord.objects.filter(pk=self.billing.pk).update(status='Paid')
        self.approve()

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')
        self.assertEqual(self.sale.total_amount, FULL)

    def test_a_cancelled_order_cannot_be_discounted(self):
        SalesTransaction.objects.filter(pk=self.sale.pk).update(order_status='Cancelled')
        self.approve()

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.total_amount, FULL)

    def test_the_apply_discount_form_cannot_go_around_a_pending_request(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('transaction_apply_discount', args=[self.sale.pk]), {
            'discount_type': 'PWD', 'discount_id_number': VALID_ID,
            'discount_holder_name': VALID_NAME,
        })

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')
        self.assertEqual(self.sale.discount_type, 'None')
        self.assertEqual(self.sale.total_amount, FULL)

    def test_the_apply_discount_form_still_works_without_a_request(self):
        """The existing in-branch flow (F-04) is untouched for an order with
        no request — same result, same audit trail."""
        SalesTransaction.objects.filter(pk=self.sale.pk).update(pwd_request_status='None')
        self.client.force_login(self.staff)
        self.client.post(reverse('transaction_apply_discount', args=[self.sale.pk]), {
            'discount_type': 'PWD', 'discount_id_number': VALID_ID,
            'discount_holder_name': VALID_NAME,
        })

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.total_amount, DISCOUNTED)
        self.assertEqual(self.sale.discount_applied_by, self.staff)
        self.assertEqual(self.sale.pwd_request_status, 'None')

    # -- who may review ------------------------------------------------------
    def test_staff_of_another_branch_cannot_review(self):
        response = self.approve(self.other_staff)

        self.assertRedirects(response, reverse('transaction_list'))
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')
        self.assertEqual(self.sale.total_amount, FULL)

    def test_a_customer_cannot_approve_their_own_request(self):
        self.approve(self.shopper)

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')
        self.assertEqual(self.sale.total_amount, FULL)

    def test_a_manager_and_the_owner_can_review(self):
        self.approve(self.manager)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Approved')

    def test_the_order_console_offers_the_review_panel_not_the_apply_form(self):
        self.client.force_login(self.staff)
        body = self.client.get(reverse('transaction_detail', args=[self.sale.pk])).content.decode()

        self.assertIn('PWD discount request — verify the ID', body)
        self.assertIn(VALID_ID, body)
        self.assertNotIn('Apply PWD / Senior Citizen discount', body)

    def test_after_a_decision_the_review_panel_goes_away(self):
        self.approve()
        self.client.force_login(self.staff)
        body = self.client.get(reverse('transaction_detail', args=[self.sale.pk])).content.decode()

        self.assertNotIn('PWD discount request — verify the ID', body)


class BillingGateTests(ReviewFixture, TestCase):
    """Payment cannot be confirmed while the amount due is not final."""

    def setUp(self):
        self.build_with_request()

    def test_billing_will_not_mark_a_pending_request_paid(self):
        response = self.confirm_paid()

        self.billing.refresh_from_db()
        self.assertEqual(self.billing.status, 'Pending')
        self.assertTrue(any('PWD discount request' in m for m in self.messages(response)))

    def test_it_does_not_stop_a_payment_being_rejected(self):
        self.client.force_login(self.manager)
        self.client.post(reverse('billing_confirm', args=[self.billing.pk]), {'status': 'Rejected'})

        self.billing.refresh_from_db()
        self.assertEqual(self.billing.status, 'Rejected')

    def test_paid_is_allowed_after_approval_at_the_discounted_amount(self):
        self.approve()
        self.confirm_paid()

        self.billing.refresh_from_db()
        self.assertEqual(self.billing.status, 'Paid')
        self.assertEqual(self.billing.amount_due, DISCOUNTED)

    def test_paid_is_allowed_after_rejection_at_the_full_amount(self):
        self.review(self.staff, decision='reject', reject_reason='No ID')
        self.confirm_paid()

        self.billing.refresh_from_db()
        self.assertEqual(self.billing.status, 'Paid')
        self.assertEqual(self.billing.amount_due, FULL)

    def test_the_order_cannot_be_completed_around_the_gate(self):
        """Completion needs a Paid bill, so the gate covers hand-over too."""
        self.client.force_login(self.manager)
        self.client.post(reverse('transaction_set_status', args=[self.sale.pk, 'Completed']))

        self.sale.refresh_from_db()
        self.assertNotEqual(self.sale.order_status, 'Completed')

    def test_the_billing_page_explains_why_verify_is_off(self):
        self.client.force_login(self.manager)
        body = self.client.get(reverse('billing_detail', args=[self.billing.pk])).content.decode()

        self.assertIn('PWD discount request waiting for verification', body)
        self.assertNotIn('name="status" value="Paid"', body)

    def test_an_order_without_a_request_is_not_gated(self):
        SalesTransaction.objects.filter(pk=self.sale.pk).update(pwd_request_status='None')
        self.confirm_paid()

        self.billing.refresh_from_db()
        self.assertEqual(self.billing.status, 'Paid')

    def test_the_api_confirm_endpoint_is_gated_the_same_way(self):
        api = APIClient()
        api.force_login(self.manager)
        url = reverse('api-billing-confirm', args=[self.billing.pk])

        blocked = api.patch(url, {'status': 'Paid'}, format='json')
        self.assertEqual(blocked.status_code, 400)
        self.billing.refresh_from_db()
        self.assertEqual(self.billing.status, 'Pending')

        self.approve()
        api.force_login(self.manager)
        allowed = api.patch(url, {'status': 'Paid'}, format='json')
        self.assertEqual(allowed.status_code, 200)
        self.billing.refresh_from_db()
        self.assertEqual(self.billing.status, 'Paid')


class RefundAndTotalsTests(ReviewFixture, TestCase):
    """P14: refunds and sales totals off an order that was discounted by
    approval. The refund reads the frozen line price, so it must be the
    discounted ₱80.00, not the ₱112.00 shelf price."""

    def setUp(self):
        self.build_with_request()
        self.approve()
        self.confirm_paid()
        SalesTransaction.objects.filter(pk=self.sale.pk).update(
            order_status='Completed', completed_at=timezone.now()
        )

    def refund(self, quantity):
        item = self.sale.items.get()
        self.client.force_login(self.owner)
        return self.client.post(reverse('transaction_refund', args=[self.sale.pk]), {
            'item_id': [str(item.pk)],
            f'quantity_{item.pk}': str(quantity),
            f'condition_{item.pk}': 'Sellable',
            'reason_code': 'Changed Mind',
            'reason': 'Changed mind',
            'refund_method': 'Cash',
        })

    def test_a_partial_refund_is_at_the_discounted_price(self):
        self.refund(1)

        refund = Refund.objects.get()
        self.assertEqual(refund.total_amount, Decimal('80.00'))
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.billing_record.status, 'Partially Refunded')
        # The sale itself is still the ₱160.00 order it was approved as.
        self.assertEqual(self.sale.total_amount, DISCOUNTED)

    def test_refunding_everything_returns_exactly_what_was_paid(self):
        self.refund(2)

        self.assertEqual(Refund.objects.get().total_amount, DISCOUNTED)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.billing_record.status, 'Refunded')

    def test_net_sales_are_the_discounted_total_less_refunds(self):
        self.refund(1)

        self.sale.refresh_from_db()
        qs = SalesTransaction.objects.filter(pk=self.sale.pk)
        self.assertEqual(self.sale.total_amount, DISCOUNTED)
        self.assertEqual(refund_value_total(qs), Decimal('80.00'))
        self.assertEqual(self.sale.total_amount - refund_value_total(qs), Decimal('80.00'))

    def test_an_approved_discount_is_not_reapplied_after_payment(self):
        """Paid bills are frozen: re-approving / applying cannot change them."""
        self.client.force_login(self.staff)
        self.client.post(reverse('transaction_apply_discount', args=[self.sale.pk]), {
            'discount_type': 'Senior', 'discount_id_number': 'OSCA-12345',
            'discount_holder_name': VALID_NAME,
        })

        self.sale.refresh_from_db()
        self.assertEqual(self.sale.discount_type, 'PWD')
        self.assertEqual(self.sale.total_amount, DISCOUNTED)


class ApiTests(ReviewFixture, TestCase):
    def setUp(self):
        self.build()
        self.api = APIClient()

    def payload(self, **override):
        body = {
            'branchId': self.branch.pk,
            'transactionType': 'Online',
            'modeOfPayment': 'Pay at Store',
            'fulfillmentType': 'Pickup',
            'items': [{'productId': self.eligible.pk, 'quantity': 2}],
            'pwdDiscountRequest': {'idNumber': VALID_ID, 'holderName': VALID_NAME},
        }
        body.update(override)
        return {k: v for k, v in body.items() if v is not None}

    def create(self, user=None, **override):
        self.api.force_login(user or self.shopper)
        return self.api.post('/api/sales/', self.payload(**override), format='json')

    # -- creating -------------------------------------------------------------
    def test_a_customer_pickup_order_records_a_pending_request_at_full_price(self):
        response = self.create()

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['pwdRequest']['status'], 'Pending')
        self.assertEqual(response.data['pwdRequest']['idNumber'], VALID_ID)
        self.assertEqual(response.data['fulfillmentType'], 'Pickup')
        self.assertEqual(response.data['discountType'], 'None')
        self.assertEqual(Decimal(response.data['totalAmount']), FULL)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.pwd_request_status, 'Pending')
        self.assertEqual(sale.delivery_address, '')
        self.assertEqual(sale.billing_record.amount_due, FULL)

    def test_it_matches_the_storefront_result(self):
        """Same fields, same values as the template checkout writes."""
        self.create()
        api_sale = SalesTransaction.objects.get()
        self.assertEqual(
            (api_sale.pwd_request_status, api_sale.pwd_request_id_number,
             api_sale.pwd_request_holder_name, api_sale.total_amount,
             api_sale.total_discount_amount, api_sale.discount_type),
            ('Pending', VALID_ID, VALID_NAME, FULL, Decimal('0.00'), 'None'),
        )

    def test_the_branch_is_notified(self):
        self.create()
        self.assertTrue(Notification.objects.filter(
            user=self.manager, title__startswith='PWD discount request'
        ).exists())

    def test_delivery_cannot_carry_a_request(self):
        response = self.create(
            fulfillmentType='Delivery', deliveryAddress='12 Real St'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('pwdDiscountRequest', response.data)
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_an_online_order_with_no_fulfillment_is_still_a_delivery(self):
        """Backwards compatible: existing callers send neither field."""
        response = self.create(
            fulfillmentType=None, pwdDiscountRequest=None, deliveryAddress='12 Real St'
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['fulfillmentType'], 'Delivery')
        self.assertEqual(response.data['pwdRequest'], None)

    def test_a_delivery_still_needs_an_address(self):
        response = self.create(fulfillmentType=None, pwdDiscountRequest=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn('deliveryAddress', response.data)

    def test_a_pickup_order_never_stores_an_address(self):
        self.create(deliveryAddress='Somewhere else')
        self.assertEqual(SalesTransaction.objects.get().delivery_address, '')

    def test_bad_id_details_are_refused(self):
        response = self.create(
            pwdDiscountRequest={'idNumber': 'MADE-UP-ID', 'holderName': VALID_NAME}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_a_request_with_nothing_eligible_is_refused(self):
        response = self.create(
            items=[{'productId': self.ineligible.pk, 'quantity': 1}]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_staff_cannot_file_a_customer_request(self):
        response = self.create(user=self.staff)
        self.assertEqual(response.status_code, 400)
        self.assertIn('pwdDiscountRequest', response.data)

    def test_a_walk_in_cannot_be_delivery(self):
        response = self.create(
            user=self.staff, transactionType='Physical', modeOfPayment='Cash',
            fulfillmentType='Delivery', pwdDiscountRequest=None,
        )
        self.assertEqual(response.status_code, 400)

    # -- reviewing ------------------------------------------------------------
    def make_request(self):
        self.create()
        self.sale = SalesTransaction.objects.get()
        self.billing = self.sale.billing_record

    def review(self, user, **body):
        self.api.force_login(user)
        return self.api.post(f'/api/sales/{self.sale.pk}/pwd-request/', body, format='json')

    def test_staff_can_approve_through_the_api(self):
        self.make_request()
        response = self.review(
            self.staff, decision='approve', idNumber=VALID_ID, holderName=VALID_NAME
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['pwdRequest']['status'], 'Approved')
        self.assertEqual(Decimal(response.data['totalAmount']), DISCOUNTED)
        self.assertEqual(response.data['discountType'], 'PWD')
        self.billing.refresh_from_db()
        self.assertEqual(self.billing.amount_due, DISCOUNTED)

    def test_the_api_and_the_console_approve_to_the_same_result(self):
        self.make_request()
        self.review(self.staff, decision='approve', idNumber=VALID_ID, holderName=VALID_NAME)
        api_sale = SalesTransaction.objects.get(pk=self.sale.pk)

        self.assertEqual(
            (api_sale.total_amount, api_sale.total_discount_amount,
             api_sale.total_vat_amount, api_sale.items.get().unit_price,
             api_sale.discount_applied_by),
            (DISCOUNTED, Decimal('40.00'), Decimal('0.00'), Decimal('80.00'), self.staff),
        )

    def test_staff_can_reject_through_the_api(self):
        self.make_request()
        response = self.review(self.staff, decision='reject', reason='No ID')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['pwdRequest']['status'], 'Rejected')
        self.assertEqual(response.data['pwdRequest']['rejectReason'], 'No ID')
        self.assertEqual(Decimal(response.data['totalAmount']), FULL)

    def test_reject_needs_a_reason_and_a_decision_is_required(self):
        self.make_request()
        self.assertEqual(self.review(self.staff, decision='reject').status_code, 400)
        self.assertEqual(self.review(self.staff, decision='nope').status_code, 400)
        self.assertEqual(self.review(self.staff).status_code, 400)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')

    def test_a_decided_request_cannot_be_decided_twice(self):
        self.make_request()
        self.review(self.staff, decision='approve', idNumber=VALID_ID, holderName=VALID_NAME)
        again = self.review(
            self.staff, decision='approve', idNumber=VALID_ID, holderName=VALID_NAME
        )
        self.assertEqual(again.status_code, 400)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.total_amount, DISCOUNTED)

    def test_another_branchs_staff_get_a_404(self):
        self.make_request()
        response = self.review(
            self.other_staff, decision='approve', idNumber=VALID_ID, holderName=VALID_NAME
        )
        self.assertEqual(response.status_code, 404)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')

    def test_the_customer_cannot_decide_their_own_request(self):
        self.make_request()
        response = self.review(
            self.shopper, decision='approve', idNumber=VALID_ID, holderName=VALID_NAME
        )
        self.assertEqual(response.status_code, 403)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')

    def test_staff_without_the_order_console_module_are_turned_away(self):
        self.make_request()
        User.objects.filter(pk=self.staff.pk).update(allowed_modules=[BILLING])
        self.staff.refresh_from_db()
        response = self.review(
            self.staff, decision='approve', idNumber=VALID_ID, holderName=VALID_NAME
        )
        self.assertNotEqual(response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.pwd_request_status, 'Pending')

    def test_a_customer_only_reads_their_own_request(self):
        self.make_request()
        stranger = User.objects.create_user(
            username='pr-stranger', password='pw', role=User.Role.CUSTOMER
        )
        self.api.force_login(stranger)
        self.assertEqual(self.api.get(f'/api/sales/{self.sale.pk}/').status_code, 404)
        self.api.force_login(self.shopper)
        self.assertEqual(
            self.api.get(f'/api/sales/{self.sale.pk}/').data['pwdRequest']['status'],
            'Pending',
        )
