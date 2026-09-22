import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import Batch
from sales.models import DiscountVerification, SalesItem, SalesTransaction
from sales.pwd_verification import decide_pwd
from sales.tests_verified_discount import DiscountFixtureMixin


class PwdWorkflowTests(DiscountFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixture()
        self.staff = User.objects.create_user(
            username='pwd-staff', password='pw', role=User.Role.STAFF,
        )
        self.staff.staff_profile.branch = self.branch
        self.staff.staff_profile.save()

    def checkout(self, *, request_pwd=False, fulfillment='Pickup', payment='Pay at Store', **extra):
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 2}
        session['checkout_token'] = 'pwd-checkout-token'
        session.save()
        payload = {
            'checkout_token': 'pwd-checkout-token',
            'fulfillment_type': fulfillment,
            'mode_of_payment': payment,
        }
        if request_pwd:
            payload['request_pwd_discount'] = 'on'
        payload.update(extra)
        return self.client.post(reverse('orders:checkout'), payload)

    def pending_order(self):
        self.checkout(request_pwd=True)
        return SalesTransaction.objects.get()

    def decision(self, order, user, decision='verify', **extra):
        self.client.force_login(user)
        payload = {
            'decision': decision,
            'discount_id_number': 'PWD-123',
            'discount_holder_name': 'Card Holder',
            'rejection_reason': 'Invalid ID',
        }
        payload.update(extra)
        return self.client.post(reverse('transaction_decide_pwd', args=[order.pk]), payload)

    def test_regular_checkout_has_no_request_or_discount(self):
        self.checkout()
        order = SalesTransaction.objects.get()
        self.assertEqual(order.pwd_verification_status, 'NONE')
        self.assertEqual(order.total_amount, Decimal('224.00'))
        self.assertFalse(DiscountVerification.objects.exists())

    def test_requested_pickup_stays_full_price_until_branch_decision(self):
        order = self.pending_order()
        self.assertEqual(order.pwd_verification_status, 'PENDING')
        self.assertEqual(order.discount_type, 'None')
        self.assertEqual(order.total_amount, Decimal('224.00'))
        self.assertEqual(order.billing_record.amount_due, Decimal('224.00'))
        self.assertEqual(order.estimated_pwd_total, Decimal('160.00'))
        self.assertEqual(order.estimated_pwd_discount, Decimal('40.00'))
        self.assertEqual(order.pwd_verification.status, 'PENDING')

    def test_customer_pages_label_estimates_as_unconfirmed(self):
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 2}
        session.save()
        cart = self.client.get(reverse('orders:cart')).content.decode()
        self.assertIn('Request PWD Discount', cart)
        self.assertIn('Estimated amount after verification', cart)
        order = self.pending_order()
        confirmation = self.client.get(reverse('orders:confirmation', args=[order.pk])).content.decode()
        self.assertIn('Subtotal before PWD verification', confirmation)
        self.assertIn('The final amount will be confirmed after verification.', confirmation)

    def test_staff_detail_shows_pending_request_and_confirmed_actions(self):
        order = self.pending_order()
        self.client.force_login(self.staff)
        page = self.client.get(reverse('transaction_detail', args=[order.pk])).content.decode()
        self.assertIn('PWD discount verification', page)
        self.assertIn('Estimated amount:', page)
        self.assertIn('Verify PWD ID?', page)
        self.assertIn('Reject PWD request?', page)

    def test_checkout_ignores_forged_discount_and_verification_fields(self):
        self.checkout(request_pwd=True, discount_type='PWD', total_amount='0.01',
                      pwd_verification_status='VERIFIED', verified_by_employee_id=self.owner.pk)
        order = SalesTransaction.objects.get()
        self.assertEqual(order.pwd_verification_status, 'PENDING')
        self.assertEqual(order.total_amount, Decimal('224.00'))
        self.assertEqual(order.discount_type, 'None')
        self.assertIsNone(order.pwd_verification.verified_by_employee)

    def test_delivery_or_gcash_request_is_refused(self):
        self.checkout(request_pwd=True, fulfillment='Delivery')
        self.assertFalse(SalesTransaction.objects.exists())
        self.checkout(request_pwd=True, payment='GCash', reference_number='fake')
        self.assertFalse(SalesTransaction.objects.exists())

    def test_verification_uses_authenticated_staff_and_reprices(self):
        order = self.pending_order()
        self.decision(order, self.staff, verified_by_employee_id=self.owner.pk,
                      branch_id=9999, total_amount='0.01')
        order.refresh_from_db()
        audit = order.pwd_verification
        self.assertEqual(order.pwd_verification_status, 'VERIFIED')
        self.assertEqual(order.total_amount, Decimal('160.00'))
        self.assertEqual(order.billing_record.amount_due, Decimal('160.00'))
        self.assertEqual(audit.verified_by_employee, self.staff)
        self.assertEqual(audit.branch, self.branch)
        self.assertIsNotNone(audit.verified_at)
        self.assertEqual(order.items.get().unit_price, Decimal('80.00'))

    def test_other_branch_and_unassigned_staff_are_refused(self):
        order = self.pending_order()
        other = Branch.objects.create(branch_name='Other', location='Other', owner=self.owner)
        self.staff.staff_profile.branch = other
        self.staff.staff_profile.save()
        self.decision(order, self.staff)
        order.refresh_from_db()
        self.assertEqual(order.pwd_verification_status, 'PENDING')
        api = self.client.post(reverse('api-sales-pwd-verification', args=[order.pk]),
                               {'decision': 'verify', 'discount_id_number': 'PWD-123',
                                'discount_holder_name': 'Card Holder'})
        self.assertIn(api.status_code, (403, 404))
        self.staff.staff_profile.branch = None
        self.staff.staff_profile.save()
        self.decision(order, self.staff)
        order.refresh_from_db()
        self.assertEqual(order.pwd_verification_status, 'PENDING')

    def test_owner_override_and_duplicate_decision(self):
        order = self.pending_order()
        self.decision(order, self.owner)
        self.decision(order, self.staff, decision='reject')
        order.refresh_from_db()
        self.assertEqual(order.pwd_verification_status, 'VERIFIED')
        self.assertEqual(order.pwd_verification.verified_by_employee, self.owner)
        self.assertEqual(order.pwd_verification.branch, self.branch)

    def test_rejection_keeps_regular_amount_and_records_reason(self):
        order = self.pending_order()
        self.decision(order, self.staff, decision='reject')
        order.refresh_from_db()
        self.assertEqual(order.pwd_verification_status, 'REJECTED')
        self.assertEqual(order.total_amount, Decimal('224.00'))
        self.assertEqual(order.billing_record.amount_due, Decimal('224.00'))
        self.assertEqual(order.pwd_verification.rejection_reason, 'Invalid ID')
        self.client.force_login(self.owner)
        self.client.post(reverse('billing_create', args=[order.pk]), {'payment_method': 'Cash'})
        bill = order.billing_record
        bill.refresh_from_db()
        self.client.post(reverse('billing_confirm', args=[bill.pk]), {'status': 'Paid'})
        bill.refresh_from_db()
        self.assertEqual(bill.status, 'Paid')
        self.assertEqual(bill.amount_due, Decimal('224.00'))

    def test_failed_verification_rolls_back_all_writes(self):
        order = self.pending_order()
        original_save = SalesItem.save

        def fail_after_save(item, *args, **kwargs):
            original_save(item, *args, **kwargs)
            raise RuntimeError('line write failed')

        with patch.object(SalesItem, 'save', fail_after_save):
            with self.assertRaises(RuntimeError):
                decide_pwd(order.pk, self.staff, 'verify', id_number='PWD-123',
                           holder_name='Card Holder')
        order.refresh_from_db()
        self.assertEqual(order.pwd_verification_status, 'PENDING')
        self.assertEqual(order.billing_record.amount_due, Decimal('224.00'))
        self.assertEqual(order.pwd_verification.status, 'PENDING')
        self.assertEqual(order.items.get().unit_price, Decimal('112.00'))

    def test_pending_blocks_payment_and_release(self):
        order = self.pending_order()
        self.client.force_login(self.staff)
        bill = BillingRecord.objects.get(transaction=order)
        self.client.post(reverse('billing_confirm', args=[bill.pk]), {'status': 'Paid'})
        bill.refresh_from_db()
        self.assertEqual(bill.status, 'Pending')
        self.client.post(reverse('transaction_set_status', args=[order.pk, 'Approved']))
        self.client.post(reverse('transaction_set_status', args=[order.pk, 'Preparing']))
        self.client.post(reverse('transaction_set_status', args=[order.pk, 'Completed']))
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'Preparing')

    def test_verified_order_requires_final_payment_before_release(self):
        order = self.pending_order()
        Batch.objects.create(product=self.product, branch=self.branch,
                             location=Batch.Location.FLOOR, quantity=50,
                             received_date=timezone.localdate())
        self.decision(order, self.staff)
        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_set_status', args=[order.pk, 'Approved']))
        self.client.post(reverse('transaction_set_status', args=[order.pk, 'Preparing']))
        self.client.post(reverse('transaction_set_status', args=[order.pk, 'Completed']))
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'Preparing')
        choices = self.client.get(reverse('billing_create', args=[order.pk])).context['payment_choices']
        self.assertIn(('Cash', 'Cash'), choices)
        self.client.post(reverse('billing_create', args=[order.pk]), {'payment_method': 'Cash'})
        bill = order.billing_record
        bill.refresh_from_db()
        self.assertEqual(bill.payment_method, 'Cash')
        self.client.post(reverse('billing_confirm', args=[bill.pk]), {'status': 'Paid'})
        bill.refresh_from_db()
        self.assertEqual(bill.status, 'Paid')
        self.client.post(reverse('transaction_set_status', args=[order.pk, 'Completed']))
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'Completed')

    def test_api_cannot_inject_audit_identity(self):
        order = self.pending_order()
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('api-sales-pwd-verification', args=[order.pk]),
            {'decision': 'verify', 'discount_id_number': 'PWD-123',
             'discount_holder_name': 'Card Holder',
             'verified_by_employee_id': self.owner.pk, 'branch_id': 9999},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(order.pwd_verification.verified_by_employee, self.staff)

    def test_api_checkout_request_and_payment_gate(self):
        self.client.force_login(self.shopper)
        response = self.client.post(
            reverse('api-sales-list'),
            data=json.dumps({
                'branchId': self.branch.pk, 'transactionType': 'Online',
                'fulfillmentType': 'Pickup', 'requestPwdDiscount': True,
                'modeOfPayment': 'Pay at Store',
                'items': [{'productId': self.product.pk, 'quantity': 2}],
                'totalAmount': '0.01', 'pwdVerificationStatus': 'VERIFIED',
            }), content_type='application/json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        order = SalesTransaction.objects.get()
        self.assertEqual(order.pwd_verification_status, 'PENDING')
        self.assertEqual(order.total_amount, Decimal('224.00'))
        bill = order.billing_record
        self.client.force_login(self.owner)
        pay = self.client.patch(
            reverse('api-billing-pay', args=[bill.pk]),
            data={'paymentMethod': 'GCash', 'referenceNumber': 'fake'},
        )
        self.assertEqual(pay.status_code, 400)
        confirm = self.client.patch(
            reverse('api-billing-confirm', args=[bill.pk]),
            data=json.dumps({'status': 'Paid'}), content_type='application/json',
        )
        self.assertEqual(confirm.status_code, 400)
        bill.refresh_from_db()
        self.assertEqual(bill.status, 'Pending')
