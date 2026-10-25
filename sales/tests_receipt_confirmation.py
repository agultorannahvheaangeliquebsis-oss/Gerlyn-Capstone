"""Delivery / pickup receipt confirmation for online orders.

Pins down the server half of the feature: who may confirm which order, that
nothing can be confirmed twice, that confirming moves the order through the
same lifecycle rules as the status control (payment gate, stock exactly once),
what the receipt PDF and notifications contain, and what the Order Console
column and filter show. The pages are exercised through the Django test client
the way a browser would.

Uploaded evidence is written to a throwaway directory, never the project's
media folder.
"""
import base64
import io
import re
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageDraw
from unittest.mock import patch

from accounts.models import BranchManagerProfile, Notification, StaffProfile, User
from audit.models import AuditLog
from billing.models import BillingRecord
from branches.models import Branch
from core.module_permissions import SALES_POS
from inventory.models import Inventory, Product, ProductPriceTier
from sales import receipts
from sales.models import (
    OrderReceiptConfirmation, OrderStatusHistory, ReceiptNumberSequence,
    SalesItem, SalesTransaction,
)

Status = SalesTransaction.OrderStatus
Delivery = SalesTransaction.FulfillmentType.DELIVERY
Pickup = SalesTransaction.FulfillmentType.PICKUP
RECEIPT_NO = re.compile(r'^GP-RCPT-\d{8}-\d{5}$')


def png_bytes(draw=True, size=(200, 80)):
    image = Image.new('RGBA', size, (0, 0, 0, 0))
    if draw:
        ImageDraw.Draw(image).line((10, 40, 190, 40), fill=(0, 0, 0, 255), width=3)
    out = io.BytesIO()
    image.save(out, 'PNG')
    return out.getvalue()


def signature_data_url(draw=True):
    return 'data:image/png;base64,' + base64.b64encode(png_bytes(draw)).decode()


def photo_upload(name='door.jpg'):
    out = io.BytesIO()
    Image.new('RGB', (64, 48), (200, 30, 30)).save(out, 'JPEG')
    return SimpleUploadedFile(name, out.getvalue(), content_type='image/jpeg')


class ReceiptBase(TestCase):
    def setUp(self):
        media = tempfile.mkdtemp(prefix='gerlyn-receipt-evidence-')
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        for name in ('proof_photo', 'signature'):
            patcher = patch.object(
                OrderReceiptConfirmation._meta.get_field(name), 'storage',
                FileSystemStorage(location=media, base_url=None),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        self.owner = User.objects.create_user(
            username='rc-owner', password='pw', role=User.Role.OWNER)
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner)
        self.other_branch = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner)
        self.manager = self._internal('rc-manager', User.Role.BRANCH_MANAGER, self.branch)
        self.staff = self._internal('rc-staff', User.Role.STAFF, self.branch)
        self.other_staff = self._internal('rc-staff2', User.Role.STAFF, self.other_branch)
        self.customer = User.objects.create_user(
            username='rc-customer', password='pw', role=User.Role.CUSTOMER,
            first_name='Juana', last_name='Cruz')
        self.other_customer = User.objects.create_user(
            username='rc-customer2', password='pw', role=User.Role.CUSTOMER)
        self.product = Product.objects.create(
            sku_code='SKU-RC', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100)
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5)

    def _internal(self, username, role, branch):
        user = User.objects.create_user(username=username, password='pw', role=role)
        if role == User.Role.BRANCH_MANAGER:
            BranchManagerProfile.objects.update_or_create(
                user=user, defaults={'manager_name': username, 'branch': branch})
        else:
            StaffProfile.objects.update_or_create(
                user=user, defaults={'staff_name': username, 'branch': branch})
        User.objects.filter(pk=user.pk).update(allowed_modules=[SALES_POS])
        user.refresh_from_db()
        return user

    def order(self, status=Status.PREPARING, fulfillment=Delivery, paid=True,
              online=True, qty=2, branch=None, customer=None, unit_price=100):
        txn = SalesTransaction.objects.create(
            branch=branch or self.branch, order_status=status,
            total_amount=unit_price * qty,
            transaction_type=(
                SalesTransaction.TransactionType.ONLINE if online
                else SalesTransaction.TransactionType.PHYSICAL),
            fulfillment_type=fulfillment,
            delivery_address='12 Mabini St, Baliuag' if fulfillment == Delivery else '',
            mode_of_payment=SalesTransaction.ModeOfPayment.COD,
            customer=customer if customer is not None else self.customer,
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=qty, unit_price=unit_price)
        # The method actually recorded against the bill is what a receipt shows.
        BillingRecord.objects.filter(transaction=txn).update(
            payment_method=BillingRecord.PaymentMethod.COD)
        if paid:
            BillingRecord.objects.filter(transaction=txn).update(
                status=BillingRecord.PaymentStatus.PAID)
        return txn

    def staff_post(self, user, txn, **overrides):
        data = {
            'received_by_name': 'Juana Cruz',
            'received_at': '',
            'notes': '',
            'signature_data': '',
        }
        data.update(overrides)
        self.client.force_login(user)
        return self.client.post(reverse('transaction_confirm_receipt', args=[txn.pk]), data)

    def confirm_as_staff(self, txn, **overrides):
        response = self.staff_post(self.staff, txn, **overrides)
        self.assertEqual(response.status_code, 302, getattr(response, 'content', b'')[:300])
        txn.refresh_from_db()
        return receipts.confirmation_of(txn)

    def customer_post(self, user, txn):
        self.client.force_login(user)
        return self.client.post(reverse('orders:confirm_received', args=[txn.pk]))

    def messages(self, response):
        return [str(m) for m in response.wsgi_request._messages]


class StaffConfirmationTests(ReceiptBase):
    def test_confirming_a_delivery_records_proof_completes_and_deducts_stock_once(self):
        txn = self.order(Status.PREPARING, Delivery, qty=2)
        before = timezone.now()
        conf = self.confirm_as_staff(
            txn, notes='Left with the guard.', signature_data=signature_data_url())

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.COMPLETED)
        self.assertGreaterEqual(txn.completed_at, before)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 48)

        self.assertRegex(conf.receipt_number, RECEIPT_NO)
        self.assertEqual(conf.received_by_name, 'Juana Cruz')
        self.assertEqual(conf.confirmed_by, self.staff)
        self.assertIsNotNone(conf.staff_confirmed_at)
        self.assertIsNotNone(conf.received_at)
        self.assertEqual(conf.notes, 'Left with the guard.')
        self.assertTrue(conf.signature)
        self.assertEqual(conf.status, OrderReceiptConfirmation.Status.STAFF)
        self.assertTrue(OrderStatusHistory.objects.filter(
            transaction=txn, from_status='Preparing', to_status='Completed',
            actor=self.staff).exists())

    def test_confirming_a_pickup_order_works_too(self):
        txn = self.order(Status.PREPARING, Pickup)
        conf = self.confirm_as_staff(txn)
        self.assertEqual(txn.order_status, Status.COMPLETED)
        self.assertRegex(conf.receipt_number, RECEIPT_NO)

    def test_manager_and_owner_can_confirm(self):
        for user in (self.manager, self.owner):
            with self.subTest(user=user.username):
                txn = self.order(Status.PREPARING)
                response = self.staff_post(user, txn)
                self.assertEqual(response.status_code, 302)
                txn.refresh_from_db()
                self.assertEqual(txn.order_status, Status.COMPLETED)
                self.assertEqual(txn.receipt_confirmation.confirmed_by, user)

    def test_received_time_defaults_to_now_and_can_be_a_past_moment(self):
        txn = self.order(Status.PREPARING)
        SalesTransaction.objects.filter(pk=txn.pk).update(
            transaction_date=timezone.now() - timedelta(hours=5))
        earlier = timezone.localtime(timezone.now() - timedelta(hours=2))
        conf = self.confirm_as_staff(txn, received_at=earlier.strftime('%Y-%m-%dT%H:%M'))
        self.assertLess(conf.received_at, conf.staff_confirmed_at - timedelta(hours=1))

    def test_receipt_numbers_run_in_sequence_for_the_day(self):
        first = self.confirm_as_staff(self.order(Status.PREPARING))
        second = self.confirm_as_staff(self.order(Status.PREPARING))
        self.assertTrue(first.receipt_number.endswith('-00001'))
        self.assertTrue(second.receipt_number.endswith('-00002'))
        self.assertEqual(first.receipt_number[:-5], second.receipt_number[:-5])

    def test_photo_is_stored_under_the_verified_type_not_the_uploaded_name(self):
        txn = self.order(Status.PREPARING)
        self.client.force_login(self.staff)
        response = self.client.post(reverse('transaction_confirm_receipt', args=[txn.pk]), {
            'received_by_name': 'Juana', 'received_at': '', 'notes': '',
            'signature_data': '', 'proof_photo': photo_upload('door.html'),
        })
        self.assertEqual(response.status_code, 302)
        conf = txn.receipt_confirmation
        self.assertTrue(conf.proof_photo.name.endswith('.jpg'), conf.proof_photo.name)


class StaffConfirmationRefusalTests(ReceiptBase):
    def assert_untouched(self, txn, status):
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, status)
        self.assertFalse(OrderReceiptConfirmation.objects.filter(transaction=txn).exists())
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)

    def test_unpaid_order_is_not_completed_and_nothing_is_recorded(self):
        txn = self.order(Status.PREPARING, paid=False)
        response = self.staff_post(self.staff, txn)

        self.assertEqual(response.status_code, 200)   # form comes back with the reason
        self.assertContains(response, 'verified as Paid')
        self.assert_untouched(txn, Status.PREPARING)
        # No receipt number was burned by the refused attempt.
        self.assertFalse(ReceiptNumberSequence.objects.exists())

    def test_only_the_hand_over_stage_can_be_confirmed(self):
        for status in (Status.PENDING, Status.APPROVED):
            with self.subTest(status=status):
                txn = self.order(status)
                self.client.force_login(self.staff)
                url = reverse('transaction_confirm_receipt', args=[txn.pk])
                self.assertEqual(self.client.get(url).status_code, 302)
                response = self.client.post(url, {'received_by_name': 'X'})
                self.assertEqual(response.status_code, 302)
                self.assert_untouched(txn, status)

    def test_cancelled_order_cannot_be_confirmed(self):
        txn = self.order(Status.CANCELLED)
        response = self.staff_post(self.staff, txn)
        self.assertEqual(response.status_code, 302)
        self.assertIn('cancelled', ' '.join(self.messages(response)))
        self.assert_untouched(txn, Status.CANCELLED)

    def test_refunded_order_cannot_be_confirmed(self):
        txn = self.order(Status.PREPARING)
        SalesTransaction.objects.filter(pk=txn.pk).update(refunded_amount=Decimal('50'))
        response = self.staff_post(self.staff, txn)
        self.assertIn('refunded', ' '.join(self.messages(response)))
        self.assert_untouched(txn, Status.PREPARING)

        other = self.order(Status.PREPARING)
        BillingRecord.objects.filter(transaction=other).update(
            status=BillingRecord.PaymentStatus.REFUNDED)
        response = self.staff_post(self.staff, other)
        self.assertIn('refunded', ' '.join(self.messages(response)))
        self.assert_untouched(other, Status.PREPARING)

    def test_walk_in_sale_has_no_hand_over_confirmation(self):
        txn = self.order(Status.PREPARING, online=False, customer=self.customer)
        response = self.staff_post(self.staff, txn)
        self.assertEqual(response.status_code, 302)
        self.assert_untouched(txn, Status.PREPARING)

    def test_cannot_be_confirmed_twice(self):
        txn = self.order(Status.PREPARING, qty=2)
        conf = self.confirm_as_staff(txn)
        number, confirmed_at = conf.receipt_number, conf.staff_confirmed_at

        response = self.staff_post(self.staff, txn, received_by_name='Someone Else')
        self.assertEqual(response.status_code, 302)
        self.assertIn('already been confirmed', ' '.join(self.messages(response)))

        conf.refresh_from_db()
        self.assertEqual(conf.receipt_number, number)
        self.assertEqual(conf.staff_confirmed_at, confirmed_at)
        self.assertEqual(conf.received_by_name, 'Juana Cruz')
        self.assertEqual(OrderReceiptConfirmation.objects.filter(transaction=txn).count(), 1)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 48)   # deducted once, not twice
        self.assertEqual(ReceiptNumberSequence.objects.get().last_value, 1)

    def test_form_input_is_validated(self):
        txn = self.order(Status.PREPARING)
        future = timezone.localtime(timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        before_order = timezone.localtime(
            txn.transaction_date - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        cases = {
            'a name is required': {'received_by_name': '  '},
            'future time': {'received_at': future},
            'before the order': {'received_at': before_order},
            'unreadable time': {'received_at': 'not-a-date'},
            'long note': {'notes': 'x' * 501},
            'not a png signature': {'signature_data': 'data:text/html;base64,PGI+'},
            'bad base64': {'signature_data': 'data:image/png;base64,@@@'},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                response = self.staff_post(self.staff, txn, **overrides)
                self.assertEqual(response.status_code, 200)
                self.assert_untouched(txn, Status.PREPARING)

    def test_non_image_upload_is_refused(self):
        txn = self.order(Status.PREPARING)
        self.client.force_login(self.staff)
        response = self.client.post(reverse('transaction_confirm_receipt', args=[txn.pk]), {
            'received_by_name': 'Juana', 'received_at': '', 'signature_data': '',
            'proof_photo': SimpleUploadedFile('x.jpg', b'<script>alert(1)</script>', 'image/jpeg'),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not a valid image')
        self.assert_untouched(txn, Status.PREPARING)

    def test_blank_signature_pad_counts_as_no_signature(self):
        txn = self.order(Status.PREPARING)
        conf = self.confirm_as_staff(txn, signature_data=signature_data_url(draw=False))
        self.assertFalse(conf.signature)


class PermissionTests(ReceiptBase):
    def test_staff_of_another_branch_cannot_confirm(self):
        txn = self.order(Status.PREPARING)
        url = reverse('transaction_confirm_receipt', args=[txn.pk])
        self.client.force_login(self.other_staff)
        self.assertEqual(self.client.get(url).status_code, 302)
        response = self.staff_post(self.other_staff, txn)
        self.assertEqual(response.status_code, 302)
        self.assertIn('another branch', ' '.join(self.messages(response)))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.PREPARING)
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

    def test_service_refuses_other_branch_even_if_the_view_were_bypassed(self):
        txn = self.order(Status.PREPARING)
        with self.assertRaisesMessage(receipts.ReceiptError, 'another branch'):
            receipts.record_staff_confirmation(
                txn.pk, self.other_staff, received_by_name='X')
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

    def test_customer_cannot_use_the_staff_form(self):
        txn = self.order(Status.PREPARING)
        response = self.staff_post(self.customer, txn)
        self.assertEqual(response.status_code, 302)
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.PREPARING)
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

    def test_anonymous_is_sent_to_login(self):
        txn = self.order(Status.PREPARING)
        response = self.client.post(reverse('transaction_confirm_receipt', args=[txn.pk]), {})
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_staff_without_edit_permission_on_orders_is_refused(self):
        txn = self.order(Status.PREPARING)
        User.objects.filter(pk=self.staff.pk).update(module_actions={SALES_POS: []})
        self.staff.refresh_from_db()
        response = self.staff_post(self.staff, txn)
        self.assertEqual(response.status_code, 403)
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.PREPARING)

    def test_customer_can_only_confirm_their_own_order(self):
        txn = self.order(Status.PREPARING)
        response = self.customer_post(self.other_customer, txn)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

        response = self.customer_post(self.customer, txn)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(receipts.confirmation_of(SalesTransaction.objects.get(pk=txn.pk)).customer_confirmed)

    def test_service_refuses_a_customer_who_does_not_own_the_order(self):
        txn = self.order(Status.PREPARING)
        with self.assertRaises(receipts.ReceiptError):
            receipts.record_customer_confirmation(txn.pk, self.other_customer)
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

    def test_staff_account_cannot_use_the_customer_endpoint(self):
        txn = self.order(Status.PREPARING)
        self.client.force_login(self.staff)
        response = self.client.post(reverse('orders:confirm_received', args=[txn.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

    def test_confirm_endpoint_is_post_only(self):
        txn = self.order(Status.PREPARING)
        self.client.force_login(self.customer)
        response = self.client.get(reverse('orders:confirm_received', args=[txn.pk]))
        self.assertEqual(response.status_code, 405)


class CustomerConfirmationTests(ReceiptBase):
    def test_customer_confirmation_is_timestamped_and_does_not_complete_the_order(self):
        txn = self.order(Status.PREPARING)
        before = timezone.now()
        response = self.customer_post(self.customer, txn)
        self.assertEqual(response.status_code, 302)

        txn.refresh_from_db()
        conf = txn.receipt_confirmation
        self.assertGreaterEqual(conf.customer_confirmed_at, before)
        self.assertEqual(conf.customer_confirmed_by, self.customer)
        self.assertEqual(conf.status, OrderReceiptConfirmation.Status.CUSTOMER)
        # Only the store's hand-over completes an order and moves its stock.
        self.assertEqual(txn.order_status, Status.PREPARING)
        self.assertIsNone(conf.receipt_number)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)

    def test_customer_cannot_confirm_twice(self):
        txn = self.order(Status.PREPARING)
        self.customer_post(self.customer, txn)
        first = SalesTransaction.objects.get(pk=txn.pk).receipt_confirmation.customer_confirmed_at

        response = self.customer_post(self.customer, txn)
        self.assertIn('already confirmed', ' '.join(self.messages(response)))
        conf = SalesTransaction.objects.get(pk=txn.pk).receipt_confirmation
        self.assertEqual(conf.customer_confirmed_at, first)
        self.assertEqual(OrderReceiptConfirmation.objects.count(), 1)

    def test_only_offered_once_the_order_is_ready_to_hand_over(self):
        for status in (Status.PENDING, Status.APPROVED, Status.CANCELLED):
            with self.subTest(status=status):
                txn = self.order(status)
                response = self.customer_post(self.customer, txn)
                self.assertEqual(response.status_code, 302)
                self.assertFalse(OrderReceiptConfirmation.objects.filter(transaction=txn).exists())

    def test_completed_order_the_store_never_recorded_cannot_be_confirmed(self):
        txn = self.order(Status.COMPLETED)
        self.customer_post(self.customer, txn)
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

    def test_refunded_order_cannot_be_confirmed(self):
        txn = self.order(Status.PREPARING)
        self.confirm_as_staff(txn)
        SalesTransaction.objects.filter(pk=txn.pk).update(refunded_amount=Decimal('50'))
        response = self.customer_post(self.customer, txn)
        self.assertIn('refunded', ' '.join(self.messages(response)))
        self.assertFalse(SalesTransaction.objects.get(pk=txn.pk).receipt_confirmation.customer_confirmed)

    def test_customer_first_then_staff_is_fully_confirmed(self):
        txn = self.order(Status.PREPARING)
        self.customer_post(self.customer, txn)
        conf = self.confirm_as_staff(txn)

        self.assertEqual(OrderReceiptConfirmation.objects.filter(transaction=txn).count(), 1)
        self.assertTrue(conf.is_fully_confirmed)
        self.assertEqual(conf.status, OrderReceiptConfirmation.Status.FULL)
        self.assertEqual(conf.fully_confirmed_at, conf.staff_confirmed_at)
        self.assertRegex(conf.receipt_number, RECEIPT_NO)
        self.assertEqual(txn.order_status, Status.COMPLETED)

    def test_staff_first_then_customer_is_fully_confirmed(self):
        txn = self.order(Status.PREPARING)
        self.confirm_as_staff(txn)
        self.assertEqual(txn.order_status, Status.COMPLETED)

        response = self.customer_post(self.customer, txn)
        self.assertEqual(response.status_code, 302)
        conf = SalesTransaction.objects.get(pk=txn.pk).receipt_confirmation
        self.assertTrue(conf.is_fully_confirmed)
        self.assertEqual(conf.status, OrderReceiptConfirmation.Status.FULL)
        self.assertEqual(conf.fully_confirmed_at, conf.customer_confirmed_at)


class NotificationAndAuditTests(ReceiptBase):
    def customer_notices(self):
        return Notification.objects.filter(user=self.customer)

    def test_staff_confirmation_notifies_the_customer_once_with_the_receipt(self):
        txn = self.order(Status.PREPARING, Delivery)
        conf = self.confirm_as_staff(txn)

        notices = list(self.customer_notices())
        # One notice for the hand-over — not that plus the generic
        # "Order X: Completed" / "Order delivered" ones the status control sends.
        self.assertEqual(len(notices), 1, [n.title for n in notices])
        notice = notices[0]
        self.assertEqual(notice.event_type, 'order_receipt_confirmed')
        self.assertIn(conf.receipt_number, notice.message)
        self.assertEqual(
            notice.action_url, reverse('orders:receipt', args=[txn.pk]))

    def test_customer_confirmation_notifies_the_customer(self):
        txn = self.order(Status.PREPARING)
        self.customer_post(self.customer, txn)
        notice = self.customer_notices().get()
        self.assertEqual(notice.event_type, 'order_receipt_confirmed')
        self.assertIn(txn.order_number, notice.title)

    def test_refused_confirmation_sends_nothing(self):
        txn = self.order(Status.PREPARING, paid=False)
        self.staff_post(self.staff, txn)
        self.assertFalse(self.customer_notices().exists())

    def test_staff_confirmation_is_audited_without_personal_data(self):
        txn = self.order(Status.PREPARING)
        conf = self.confirm_as_staff(txn, notes='gate code 4412')

        created = AuditLog.objects.get(
            entity='sales.OrderReceiptConfirmation', record_id=str(conf.pk), action='create')
        self.assertEqual(created.actor_id, self.staff.pk)
        self.assertEqual(created.after['received_by_name'], '[personal data]')
        self.assertEqual(created.after['notes'], '[personal data]')
        self.assertEqual(created.action_label, 'Confirmed an order hand-over')

        readable = AuditLog.objects.filter(
            entity='sales.SalesTransaction', record_id=str(txn.pk),
            summary__contains='hand-over confirmed')
        self.assertEqual(readable.count(), 1)
        self.assertIn(conf.receipt_number, readable.get().summary)
        # The status change itself is on the order's history too.
        self.assertTrue(AuditLog.objects.filter(
            entity='sales.SalesTransaction', record_id=str(txn.pk),
            action='update', summary__contains='order_status').exists())
        self.assertNotIn('Juana', ' '.join(AuditLog.objects.values_list('summary', flat=True)))


class ReceiptPageAndPdfTests(ReceiptBase):
    def confirmed(self, **kwargs):
        txn = self.order(Status.PREPARING, **kwargs)
        self.client.force_login(self.staff)
        self.client.post(reverse('transaction_confirm_receipt', args=[txn.pk]), {
            'received_by_name': 'Juana Cruz', 'received_at': '',
            'notes': 'Handed to the owner', 'signature_data': signature_data_url(),
            'proof_photo': photo_upload(),
        })
        txn.refresh_from_db()
        return txn, txn.receipt_confirmation

    def assert_real_pdf(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        body = response.content
        self.assertTrue(body.startswith(b'%PDF-'))
        self.assertIn(b'%%EOF', body[-2048:])

    def test_staff_can_download_a_real_pdf_named_after_the_receipt(self):
        txn, conf = self.confirmed()
        self.client.force_login(self.staff)
        response = self.client.get(reverse('transaction_delivery_receipt_pdf', args=[txn.pk]))
        self.assert_real_pdf(response)
        self.assertEqual(
            response['Content-Disposition'], f'attachment; filename="{conf.receipt_number}.pdf"')
        self.assertGreater(len(response.content), 3000)

    def test_customer_can_download_their_own_pdf_and_nobody_elses(self):
        txn, conf = self.confirmed()
        url = reverse('orders:receipt_pdf', args=[txn.pk])
        self.client.force_login(self.customer)
        self.assert_real_pdf(self.client.get(url))
        self.client.force_login(self.other_customer)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_pdf_still_builds_when_the_evidence_files_are_unreadable(self):
        txn, conf = self.confirmed()
        for name in ('signature', 'proof_photo'):
            field_file = getattr(conf, name)
            field_file.storage.delete(field_file.name)
        self.client.force_login(self.staff)
        self.assert_real_pdf(
            self.client.get(reverse('transaction_delivery_receipt_pdf', args=[txn.pk])))

    def test_pdf_with_evidence_is_larger_than_without(self):
        with_evidence, _ = self.confirmed()
        bare = self.order(Status.PREPARING)
        self.client.force_login(self.staff)
        self.client.post(reverse('transaction_confirm_receipt', args=[bare.pk]), {
            'received_by_name': 'Juana Cruz', 'received_at': '', 'signature_data': ''})
        big = self.client.get(reverse('transaction_delivery_receipt_pdf', args=[with_evidence.pk]))
        small = self.client.get(reverse('transaction_delivery_receipt_pdf', args=[bare.pk]))
        self.assertGreater(len(big.content), len(small.content))

    def test_pdf_template_carries_everything_the_receipt_promises(self):
        ProductPriceTier.objects.create(product=self.product, min_quantity=10, unit_price=90)
        txn = self.order(Status.PREPARING, qty=10, unit_price=90)
        SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=2, unit_price=100)
        self.confirm_as_staff(txn)
        conf = txn.receipt_confirmation

        html = render_to_string('sales/receipt_pdf.html', receipts.receipt_context(txn, conf))
        for expected in (
            conf.receipt_number, txn.order_number, 'Rice (Sack)', 'Wholesale', 'Retail',
            'PHP 90.00', 'PHP 100.00', 'Cash on Delivery', 'Tarcan', 'rc-staff',
            'Juana Cruz', 'DELIVERY RECEIPT', '12 Mabini St',
        ):
            self.assertIn(expected, html)
        self.assertRegex(html, r'Date and time received')

    def test_on_screen_receipt_for_staff_and_customer(self):
        txn, conf = self.confirmed()
        self.client.force_login(self.staff)
        staff_page = self.client.get(reverse('transaction_delivery_receipt', args=[txn.pk]))
        self.assertContains(staff_page, conf.receipt_number)
        self.assertContains(staff_page, 'Download PDF')
        self.client.force_login(self.customer)
        customer_page = self.client.get(reverse('orders:receipt', args=[txn.pk]))
        self.assertContains(customer_page, conf.receipt_number)
        self.assertContains(customer_page, 'Juana Cruz')

    def test_no_receipt_until_the_store_records_the_hand_over(self):
        txn = self.order(Status.PREPARING)
        self.customer_post(self.customer, txn)     # customer-first: row exists, no receipt
        for route, user in (
            ('orders:receipt', self.customer), ('orders:receipt_pdf', self.customer),
        ):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse(route, args=[txn.pk])).status_code, 302)
        self.client.force_login(self.staff)
        for route in ('transaction_delivery_receipt', 'transaction_delivery_receipt_pdf'):
            self.assertEqual(self.client.get(reverse(route, args=[txn.pk])).status_code, 302)

    def test_receipt_is_not_readable_across_branches(self):
        txn, _ = self.confirmed()
        self.client.force_login(self.other_staff)
        for route in ('transaction_delivery_receipt', 'transaction_delivery_receipt_pdf'):
            response = self.client.get(reverse(route, args=[txn.pk]))
            self.assertEqual(response.status_code, 302)
            self.assertIn('another branch', ' '.join(self.messages(response)))

    def test_evidence_is_served_only_to_the_order_s_staff_and_customer(self):
        txn, conf = self.confirmed()
        staff_url = reverse('transaction_receipt_evidence', args=[txn.pk, 'signature'])
        customer_url = reverse('orders:receipt_evidence', args=[txn.pk, 'photo'])

        self.client.force_login(self.staff)
        response = self.client.get(staff_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(self.client.get(
            reverse('transaction_receipt_evidence', args=[txn.pk, 'photo']))['Content-Type'],
            'image/jpeg')
        self.assertEqual(self.client.get(
            reverse('transaction_receipt_evidence', args=[txn.pk, 'other'])).status_code, 404)

        self.client.force_login(self.other_staff)
        self.assertEqual(self.client.get(staff_url).status_code, 404)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(customer_url).status_code, 200)
        self.assertEqual(self.client.get(staff_url).status_code, 404)   # a staff-only route
        self.client.force_login(self.other_customer)
        self.assertEqual(self.client.get(customer_url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(staff_url).status_code, 302)

    def test_raw_media_path_is_not_public(self):
        _, conf = self.confirmed()
        self.client.logout()
        self.assertEqual(self.client.get(f'/media/{conf.signature.name}').status_code, 404)


class PriceBasisTests(ReceiptBase):
    def test_retail_wholesale_and_unknown(self):
        ProductPriceTier.objects.create(product=self.product, min_quantity=10, unit_price=90)
        txn = self.order(Status.PREPARING)
        product = Product.objects.prefetch_related('price_tiers').get(pk=self.product.pk)

        def line(qty, price):
            item = SalesItem(transaction=txn, product=product, quantity_sold=qty, unit_price=price)
            return receipts.price_basis(item, 'None')

        self.assertEqual(line(2, 100), 'Retail')
        self.assertEqual(line(10, 90), 'Wholesale')
        self.assertEqual(line(2, 77), '')          # repriced since: no guess

    def test_statutory_discount_line_is_matched_against_the_same_ladder(self):
        from sales.pricing import price_line
        product = Product.objects.prefetch_related('price_tiers').get(pk=self.product.pk)
        Product.objects.filter(pk=product.pk).update(is_pwd_eligible=True)
        product.refresh_from_db()
        net = price_line(product, 2, discount_type='PWD')['unit_price']
        txn = self.order(Status.PREPARING)
        item = SalesItem(transaction=txn, product=product, quantity_sold=2, unit_price=net)
        self.assertEqual(receipts.price_basis(item, 'PWD'), 'Retail')


class ConsolePagesTests(ReceiptBase):
    def listing(self, **params):
        self.client.force_login(self.owner)
        return self.client.get(reverse('transaction_list'), params)

    def numbers(self, response):
        return {t.order_number for t in response.context['transactions']}

    def test_filter_by_confirmation_status(self):
        none = self.order(Status.APPROVED)
        staff_only = self.order(Status.PREPARING)
        self.confirm_as_staff(staff_only)
        customer_only = self.order(Status.PREPARING)
        self.customer_post(self.customer, customer_only)
        full = self.order(Status.PREPARING)
        self.customer_post(self.customer, full)
        self.confirm_as_staff(full)
        cancelled = self.order(Status.CANCELLED)
        walk_in = self.order(Status.COMPLETED, online=False)

        def of(code):
            return self.numbers(self.listing(receipt=code))

        self.assertEqual(of('none'), {none.order_number})
        self.assertEqual(of('staff'), {staff_only.order_number})
        self.assertEqual(of('customer'), {customer_only.order_number})
        self.assertEqual(of('full'), {full.order_number})
        everything = self.numbers(self.listing())
        self.assertTrue({cancelled.order_number, walk_in.order_number} <= everything)
        # A hand-edited value is ignored, not an error.
        self.assertEqual(self.numbers(self.listing(receipt='bogus')), everything)

    def test_column_header_pills_and_dashes(self):
        full = self.order(Status.PREPARING)
        self.customer_post(self.customer, full)
        self.confirm_as_staff(full)
        self.order(Status.APPROVED)
        self.order(Status.CANCELLED)
        response = self.listing()
        self.assertContains(response, '<th>Confirmation</th>', html=True)
        self.assertContains(response, 'Fully confirmed')
        self.assertContains(response, 'Not confirmed')
        self.assertContains(response, 'name="receipt"')

    def test_filter_is_carried_by_the_status_tabs(self):
        response = self.listing(receipt='full')
        self.assertIn('receipt=full', response.context['filter_qs'])

    def test_branch_manager_only_sees_their_own_branch_in_the_filter(self):
        mine = self.order(Status.PREPARING)
        self.confirm_as_staff(mine)
        elsewhere = self.order(Status.PREPARING, branch=self.other_branch)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('transaction_list'), {'receipt': 'staff'})
        self.assertEqual(self.numbers(response), {mine.order_number})
        self.assertNotIn(elsewhere.order_number, self.numbers(response))

    def test_staff_order_detail_offers_confirm_then_receipt_links(self):
        txn = self.order(Status.PREPARING, Delivery)
        self.client.force_login(self.staff)
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertContains(page, reverse('transaction_confirm_receipt', args=[txn.pk]))
        self.assertContains(page, 'Confirm Delivery')

        pickup = self.order(Status.PREPARING, Pickup)
        self.assertContains(
            self.client.get(reverse('transaction_detail', args=[pickup.pk])), 'Confirm Receipt')

        self.confirm_as_staff(txn)
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertNotContains(page, reverse('transaction_confirm_receipt', args=[txn.pk]))
        self.assertContains(page, reverse('transaction_delivery_receipt', args=[txn.pk]))
        self.assertContains(page, reverse('transaction_delivery_receipt_pdf', args=[txn.pk]))

    def test_confirm_button_hidden_for_unpaid_stages_and_walk_ins(self):
        approved = self.order(Status.APPROVED)
        walk_in = self.order(Status.PREPARING, online=False)
        self.client.force_login(self.staff)
        for txn in (approved, walk_in):
            page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
            self.assertNotContains(page, reverse('transaction_confirm_receipt', args=[txn.pk]))

    def test_track_order_page_for_the_customer(self):
        txn = self.order(Status.PREPARING)
        self.client.force_login(self.customer)
        url = reverse('orders:track_order', args=[txn.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Confirm I received my order')
        self.assertContains(page, reverse('orders:confirm_received', args=[txn.pk]))
        self.assertNotContains(page, 'Download PDF')

        approved = self.order(Status.APPROVED)
        self.assertNotContains(
            self.client.get(reverse('orders:track_order', args=[approved.pk])),
            'Confirm I received my order')

        self.confirm_as_staff(txn)
        self.client.force_login(self.customer)
        page = self.client.get(url)
        self.assertContains(page, 'Confirm I received my order')   # staff first: customer can still confirm
        self.assertContains(page, 'View Receipt')
        self.assertContains(page, 'Download PDF')

        self.customer_post(self.customer, txn)
        page = self.client.get(url)
        self.assertNotContains(page, 'Confirm I received my order')
        self.assertContains(page, 'Download PDF')


class OnlineOrdersAreCompletedByTheirHandOverTests(ReceiptBase):
    """The status control no longer completes an online order: doing so would
    finish it, deduct its stock and start its refund window with no proof of
    hand-over on record. sales.receipts is the only way to Completed."""

    def test_next_actions_leave_completed_out_for_online_and_keep_it_for_walk_ins(self):
        from sales.views import _next_actions
        online = self.order(Status.PREPARING, online=True)
        walk_in = self.order(Status.PENDING, online=False)
        self.assertEqual(_next_actions(online, self.owner), ['Cancelled'])
        self.assertEqual(_next_actions(walk_in, self.owner), ['Completed', 'Cancelled'])

    def test_the_status_endpoint_refuses_every_role(self):
        for user in (self.owner, self.manager, self.staff):
            with self.subTest(user=user.username):
                txn = self.order(Status.PREPARING)
                self.client.force_login(user)
                response = self.client.post(
                    reverse('transaction_set_status', args=[txn.pk, 'Completed']))
                self.assertEqual(response.status_code, 302)
                txn.refresh_from_db()
                self.assertEqual(txn.order_status, Status.PREPARING)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)

    def test_a_walk_in_still_completes_from_the_status_endpoint(self):
        txn = self.order(Status.PENDING, online=False)
        self.client.force_login(self.staff)
        self.client.post(reverse('transaction_set_status', args=[txn.pk, 'Completed']))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.COMPLETED)

    def test_paying_an_online_order_points_to_the_hand_over_not_to_mark_completed(self):
        txn = self.order(Status.PREPARING, paid=False)
        BillingRecord.objects.filter(transaction=txn).update(
            payment_method=BillingRecord.PaymentMethod.CASH)
        record = txn.billing_record
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('billing_confirm', args=[record.pk]), {'status': 'Paid'}, follow=True)
        text = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('payment marked as Paid', text)
        self.assertIn('delivery or pickup can now be confirmed', text)
        self.assertNotIn('marked Completed', text)


class LateRecordingTests(ReceiptBase):
    """Orders completed before the rule above have no hand-over on record.
    Staff can record it afterwards; the order itself is left exactly as it is."""

    def legacy(self, **kwargs):
        return self.order(Status.COMPLETED, **kwargs)

    def test_recording_late_issues_a_receipt_and_changes_nothing_else(self):
        txn = self.legacy()
        SalesTransaction.objects.filter(pk=txn.pk).update(
            completed_at=timezone.now() - timedelta(days=1))
        txn.refresh_from_db()
        completed_at = txn.completed_at
        history_before = OrderStatusHistory.objects.filter(transaction=txn).count()

        response = self.staff_post(self.staff, txn)
        self.assertEqual(response.status_code, 302)

        txn.refresh_from_db()
        conf = txn.receipt_confirmation
        self.assertRegex(conf.receipt_number, RECEIPT_NO)
        self.assertEqual(conf.confirmed_by, self.staff)
        self.assertEqual(conf.status, OrderReceiptConfirmation.Status.STAFF)
        # The order is exactly as it was: no step, no stock, no new window.
        self.assertEqual(txn.order_status, Status.COMPLETED)
        self.assertEqual(txn.completed_at, completed_at)
        self.assertEqual(OrderStatusHistory.objects.filter(transaction=txn).count(), history_before)
        self.assertFalse(txn.status_changes.exists())
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)

    def test_the_message_does_not_claim_the_order_was_just_completed(self):
        txn = self.legacy()
        response = self.staff_post(self.staff, txn)
        text = ' '.join(self.messages(response))
        self.assertIn('issued', text)
        self.assertNotIn('now Completed', text)

    def test_it_is_audited_as_a_late_recording(self):
        txn = self.legacy()
        conf = self.confirm_as_staff(txn)
        summary = AuditLog.objects.get(
            entity='sales.SalesTransaction', record_id=str(txn.pk),
            summary__contains='hand-over recorded').summary
        self.assertIn('after the order was completed', summary)
        self.assertIn(conf.receipt_number, summary)
        self.assertIn('order status unchanged', summary)

    def test_no_payment_check_applies_to_a_step_that_already_happened(self):
        txn = self.legacy(paid=False)
        self.client.force_login(self.staff)
        form = self.client.get(reverse('transaction_confirm_receipt', args=[txn.pk]))
        self.assertNotContains(form, 'verified as Paid')
        self.assertNotRegex(form.content.decode(), r'<button type="submit"[^>]*\sdisabled')
        self.assertContains(form, 'already <strong>Completed</strong>')
        self.confirm_as_staff(txn)
        self.assertEqual(txn.order_status, Status.COMPLETED)

    def test_the_customer_is_notified_and_can_then_confirm(self):
        txn = self.legacy()
        conf = self.confirm_as_staff(txn)
        notice = Notification.objects.get(user=self.customer)
        self.assertEqual(notice.event_type, 'order_receipt_confirmed')
        self.assertIn(conf.receipt_number, notice.message)

        self.customer_post(self.customer, txn)
        conf = SalesTransaction.objects.get(pk=txn.pk).receipt_confirmation
        self.assertEqual(conf.status, OrderReceiptConfirmation.Status.FULL)

    def test_a_customer_who_confirmed_before_it_was_completed_gets_the_same_row(self):
        txn = self.order(Status.PREPARING)
        self.customer_post(self.customer, txn)
        # Completed some other way (the old status control) after that.
        SalesTransaction.objects.filter(pk=txn.pk).update(order_status=Status.COMPLETED)
        conf = self.confirm_as_staff(txn)
        self.assertEqual(OrderReceiptConfirmation.objects.filter(transaction=txn).count(), 1)
        self.assertTrue(conf.is_fully_confirmed)
        self.assertEqual(txn.order_status, Status.COMPLETED)

    def test_it_is_still_once_only_and_refused_when_refunded_cancelled_or_a_walk_in(self):
        txn = self.legacy()
        self.confirm_as_staff(txn)
        again = self.staff_post(self.staff, txn)
        self.assertIn('already been confirmed', ' '.join(self.messages(again)))

        refunded = self.legacy()
        SalesTransaction.objects.filter(pk=refunded.pk).update(refunded_amount=Decimal('20'))
        self.assertIn('refunded', ' '.join(self.messages(self.staff_post(self.staff, refunded))))

        walk_in = self.order(Status.COMPLETED, online=False)
        self.staff_post(self.staff, walk_in)
        cancelled = self.order(Status.CANCELLED)
        self.staff_post(self.staff, cancelled)
        for blocked in (refunded, walk_in, cancelled):
            self.assertFalse(OrderReceiptConfirmation.objects.filter(transaction=blocked).exists())

    def test_other_branches_still_cannot_do_it(self):
        txn = self.legacy()
        response = self.staff_post(self.other_staff, txn)
        self.assertIn('another branch', ' '.join(self.messages(response)))
        self.assertFalse(OrderReceiptConfirmation.objects.exists())

    def test_the_detail_page_offers_it_then_stops(self):
        txn = self.legacy()
        self.client.force_login(self.staff)
        url = reverse('transaction_confirm_receipt', args=[txn.pk])
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertContains(page, url)
        self.assertContains(page, 'completed without a delivery or pickup confirmation')

        self.confirm_as_staff(txn)
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertNotContains(page, url)
        self.assertContains(page, reverse('transaction_delivery_receipt_pdf', args=[txn.pk]))

    def test_legacy_orders_are_found_by_the_completed_tab_and_not_confirmed_filter(self):
        legacy = self.legacy()
        done = self.legacy()
        self.confirm_as_staff(done)
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('transaction_list'), {'status': 'Completed', 'receipt': 'none'})
        self.assertEqual(
            {t.order_number for t in response.context['transactions']}, {legacy.order_number})


class VoidedReceiptTests(ReceiptBase):
    def test_a_receipt_for_a_voided_sale_says_so(self):
        txn = self.order(Status.PREPARING)
        conf = self.confirm_as_staff(txn)
        self.client.force_login(self.owner)
        self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'reason': 'Wrong goods'})
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.CANCELLED)

        page = self.client.get(reverse('transaction_delivery_receipt', args=[txn.pk]))
        self.assertContains(page, 'later cancelled (voided)')
        html = render_to_string('sales/receipt_pdf.html', receipts.receipt_context(txn, conf))
        self.assertIn('later cancelled (voided)', html)

        live = self.confirm_as_staff(self.order(Status.PREPARING))
        live_html = render_to_string(
            'sales/receipt_pdf.html', receipts.receipt_context(live.transaction, live))
        self.assertNotIn('later cancelled (voided)', live_html)
