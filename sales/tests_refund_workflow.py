"""P14 — the refund approval workflow.

A refund is a request. Staff (or anyone with the branch) raises it; it sits
Pending — reserving its units, moving nothing — until an Owner/Branch Manager
approves or rejects it. Approval is the one place stock is restocked, the
order's refunded_amount grows and billing steps to Partially Refunded /
Refunded, all in one transaction. sales.tests.RefundTests covers the
Owner/Manager self-approving path that predates the workflow; this file covers
what P14 added: approval states, reason codes, the event log, the order status
trail, voids, reports and the API.
"""
import importlib
import json
import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.apps import apps as django_apps
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import BranchManagerProfile, Notification, StaffProfile, User
from billing.models import BillingRecord
from branches.models import Branch
from core.module_permissions import SALES_POS
from inventory.models import Batch, Inventory, Product, StockMovement
from reports.services import refund_value_total
from sales import refunds as refund_service
from sales.models import (
    OrderStatusHistory, Refund, RefundEvent, RefundItem, SalesItem, SalesTransaction,
)
from sales.serializers import SalesTransactionSerializer


class RefundWorkflowBase:
    """Shared fixtures — two branches, a manager and a staff member on each."""

    def make_users(self):
        self.owner = User.objects.create_user(
            username='wf-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.other_branch = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.manager = self._manager('wf-manager', self.branch)
        self.other_manager = self._manager('wf-manager2', self.other_branch)
        self.staff = self._staff('wf-staff', self.branch)
        self.other_staff = self._staff('wf-staff2', self.other_branch)
        self.customer = User.objects.create_user(
            username='wf-customer', password='pw', role=User.Role.CUSTOMER
        )
        self.product = Product.objects.create(
            sku_code='SKU-WF', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )

    def _manager(self, username, branch):
        user = User.objects.create_user(username=username, password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=user, defaults={'manager_name': username, 'branch': branch}
        )
        return user

    def _staff(self, username, branch):
        user = User.objects.create_user(username=username, password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=user, defaults={'staff_name': username, 'branch': branch}
        )
        User.objects.filter(pk=user.pk).update(allowed_modules=[SALES_POS])
        user.refresh_from_db()
        return user

    def order(self, quantity=5, unit_price=Decimal('100'), branch=None, total=None, customer=None):
        txn = SalesTransaction.objects.create(
            branch=branch or self.branch,
            order_status=SalesTransaction.OrderStatus.COMPLETED,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            total_amount=total if total is not None else quantity * unit_price,
            completed_at=timezone.now(),
            customer=customer,
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product,
            quantity_sold=quantity, unit_price=unit_price,
        )
        BillingRecord.objects.filter(transaction=txn).update(
            status=BillingRecord.PaymentStatus.PAID
        )
        return txn

    def payload(self, item, quantity, condition='Sellable', reason_code='Defective',
                reason='', refund_method='Cash'):
        return {
            'item_id': [str(item.pk)],
            f'quantity_{item.pk}': str(quantity),
            f'condition_{item.pk}': condition,
            'reason_code': reason_code,
            'reason': reason,
            'refund_method': refund_method,
        }

    def request_as(self, user, txn, item, quantity, **kw):
        self.client.force_login(user)
        return self.client.post(
            reverse('transaction_refund', args=[txn.pk]), self.payload(item, quantity, **kw)
        )

    def decide_as(self, user, refund, approve=True, note=''):
        self.client.force_login(user)
        name = 'transaction_refund_approve' if approve else 'transaction_refund_reject'
        return self.client.post(reverse(name, args=[refund.pk]), {'decision_note': note})

    def flash(self, response):
        return [str(m.message) for m in response.wsgi_request._messages]

    def stock(self):
        self.inventory.refresh_from_db()
        return self.inventory.quantity_on_hand

    def billing_status(self, txn):
        return BillingRecord.objects.get(transaction=txn).status


@mock.patch('core.notifications.notify')
class RefundApprovalTests(RefundWorkflowBase, TestCase):
    def setUp(self):
        self.make_users()

    # -- request: reserves, moves nothing ---------------------------------------

    def test_a_staff_request_is_pending_and_moves_nothing(self, _notify):
        txn = self.order()
        item = txn.items.get()
        before = self.stock()

        self.request_as(self.staff, txn, item, 2)

        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(refund.refund_type, Refund.RefundType.PARTIAL)
        self.assertEqual(refund.reason_code, 'Defective')
        self.assertEqual(refund.requested_by, self.staff)
        self.assertIsNone(refund.refunded_by)
        self.assertIsNone(refund.refunded_at)
        self.assertEqual(self.stock(), before)
        self.assertFalse(StockMovement.objects.filter(movement_type='Customer Return').exists())
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('0'))
        self.assertEqual(self.billing_status(txn), 'Paid')
        self.assertEqual(
            list(refund.events.values_list('from_status', 'to_status')), [('', 'Pending')]
        )

    def test_a_pending_request_reserves_its_units_and_a_rejection_releases_them(self, _notify):
        txn = self.order(quantity=5)
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 4)
        self.assertEqual(refund_service.refundable_lines(txn)[0]['remaining'], 1)

        over = self.request_as(self.staff, txn, item, 2)
        self.assertTrue(any('Only 1 unit' in m for m in self.flash(over)))
        self.assertEqual(Refund.objects.count(), 1)

        self.decide_as(self.manager, Refund.objects.get(), approve=False, note='Not returned')
        self.assertEqual(refund_service.refundable_lines(txn)[0]['remaining'], 5)

    def test_reason_code_is_validated_and_other_needs_a_note(self, _notify):
        txn = self.order()
        item = txn.items.get()

        bad = self.request_as(self.staff, txn, item, 1, reason_code='Because')
        self.assertTrue(any('Choose a reason' in m for m in self.flash(bad)))

        other = self.request_as(self.staff, txn, item, 1, reason_code='Other', reason='')
        self.assertTrue(any('"Other" needs a note' in m for m in self.flash(other)))
        self.assertEqual(Refund.objects.count(), 0)

        self.request_as(self.staff, txn, item, 1, reason_code='Other', reason='Box was crushed')
        self.assertEqual(Refund.objects.get().reason, 'Box was crushed')

    def test_a_staff_member_from_another_branch_cannot_request(self, _notify):
        txn = self.order()
        item = txn.items.get()

        response = self.request_as(self.other_staff, txn, item, 1)

        self.assertEqual(Refund.objects.count(), 0)
        self.assertTrue(any('another branch' in m for m in self.flash(response)))

    def test_an_unassigned_staff_member_fails_closed(self, _notify):
        txn = self.order()
        item = txn.items.get()
        StaffProfile.objects.filter(user=self.staff).update(branch=None)

        self.request_as(self.staff, txn, item, 1)

        self.assertEqual(Refund.objects.count(), 0)

    def test_the_amount_is_derived_exactly_from_the_frozen_unit_price(self, _notify):
        txn = self.order(quantity=3, unit_price=Decimal('33.33'))
        item = txn.items.get()

        self.request_as(self.manager, txn, item, 2)

        refund = Refund.objects.get()
        self.assertEqual(refund.total_amount, Decimal('66.66'))
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('66.66'))
        self.assertEqual(txn.total_amount, Decimal('99.99'))     # never reduced
        self.assertEqual(txn.net_amount, Decimal('33.33'))

    def test_a_refund_can_never_exceed_what_the_order_was_charged(self, _notify):
        # Lines add up to 500 but only 100 was ever charged (a legacy total).
        txn = self.order(quantity=5, total=Decimal('100'))
        item = txn.items.get()

        response = self.request_as(self.manager, txn, item, 2)

        self.assertEqual(Refund.objects.count(), 0)
        self.assertTrue(any('more than' in m for m in self.flash(response)))

    # -- approve: the single place things move -----------------------------------

    def test_approval_restocks_records_the_money_and_steps_billing(self, _notify):
        txn = self.order(quantity=5)
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2, condition='Sellable')
        refund = Refund.objects.get()
        before = self.stock()

        response = self.decide_as(self.manager, refund, approve=True, note='Checked the goods')

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertEqual(refund.refunded_by, self.manager)
        self.assertIsNotNone(refund.refunded_at)
        self.assertEqual(self.stock(), before + 2)
        movement = StockMovement.objects.get(movement_type='Customer Return')
        self.assertEqual(
            (movement.quantity_change, movement.quantity_before, movement.quantity_after,
             movement.created_by, movement.reference_id),
            (2, before, before + 2, self.manager, txn.pk),
        )
        self.assertIn(f'Refund #{refund.pk}', movement.notes)
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('200.00'))
        self.assertEqual(txn.total_amount, Decimal('500.00'))
        self.assertEqual(txn.net_amount, Decimal('300.00'))
        self.assertEqual(self.billing_status(txn), 'Partially Refunded')
        self.assertEqual(txn.order_status, 'Completed')
        events = list(refund.events.order_by('id'))
        self.assertEqual([e.to_status for e in events], ['Pending', 'Approved'])
        self.assertEqual(events[1].actor, self.manager)
        self.assertIn('Paid -> Partially Refunded', events[1].note)
        self.assertIn('Checked the goods', events[1].note)

    def test_a_damaged_line_is_written_off_but_still_counts_as_refunded(self, _notify):
        txn = self.order(quantity=5)
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2, condition='Damaged')
        before = self.stock()

        self.decide_as(self.manager, Refund.objects.get())

        self.assertEqual(self.stock(), before)
        self.assertFalse(StockMovement.objects.filter(movement_type='Customer Return').exists())
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('200.00'))

    def test_a_full_refund_is_typed_full_and_closes_the_billing_record(self, _notify):
        txn = self.order(quantity=5)
        item = txn.items.get()
        self.client.force_login(self.owner)

        self.client.post(reverse('transaction_refund', args=[txn.pk]), {
            'refund_all': '1', f'condition_{item.pk}': 'Sellable',
            'reason_code': 'Wrong Item', 'refund_method': 'GCash',
        })

        refund = Refund.objects.get()
        self.assertEqual(refund.refund_type, Refund.RefundType.FULL)
        self.assertEqual(refund.status, Refund.Status.APPROVED)     # owner: self-approved
        self.assertEqual(refund.total_amount, Decimal('500.00'))
        txn.refresh_from_db()
        self.assertEqual(txn.net_amount, Decimal('0.00'))
        self.assertEqual(self.billing_status(txn), 'Refunded')
        self.assertEqual(refund_service.refundable_lines(txn), [])

    def test_a_partial_then_final_refund_ends_at_refunded(self, _notify):
        txn = self.order(quantity=5)
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        first = Refund.objects.get()
        self.decide_as(self.manager, first)
        self.assertEqual(self.billing_status(txn), 'Partially Refunded')

        self.request_as(self.staff, txn, item, 3)
        second = Refund.objects.exclude(pk=first.pk).get()
        self.assertEqual(second.refund_type, Refund.RefundType.PARTIAL)   # not the whole order in one go
        self.decide_as(self.manager, second)

        self.assertEqual(self.billing_status(txn), 'Refunded')
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('500.00'))

    def test_a_pending_request_does_not_step_billing_until_approved(self, _notify):
        txn = self.order(quantity=2)
        item = txn.items.get()

        self.request_as(self.staff, txn, item, 2)      # every unit — but only requested

        self.assertEqual(self.billing_status(txn), 'Paid')

    def test_a_request_cannot_be_decided_twice(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        refund = Refund.objects.get()
        before = self.stock()
        self.decide_as(self.manager, refund)

        again = self.decide_as(self.owner, refund)
        rejected_after = self.decide_as(self.owner, refund, approve=False, note='Changed my mind')

        self.assertTrue(any('already approved' in m for m in self.flash(again)))
        self.assertTrue(any('already approved' in m for m in self.flash(rejected_after)))
        self.assertEqual(self.stock(), before + 2)             # restocked exactly once
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('200.00'))
        self.assertEqual(refund.events.count(), 2)

    def test_approval_is_refused_and_rolled_back_once_the_order_is_no_longer_paid(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        BillingRecord.objects.filter(transaction=txn).update(
            status=BillingRecord.PaymentStatus.CANCELLED
        )
        before = self.stock()

        response = self.decide_as(self.manager, Refund.objects.get())

        self.assertTrue(any('can no longer be refunded' in m for m in self.flash(response)))
        self.assertEqual(Refund.objects.get().status, Refund.Status.PENDING)
        self.assertEqual(self.stock(), before)
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('0'))

    def test_a_sellable_line_whose_product_was_deleted_is_recorded_but_not_restocked(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        Product.objects.filter(pk=self.product.pk).delete()

        response = self.decide_as(self.manager, Refund.objects.get())

        self.assertEqual(Refund.objects.get().status, Refund.Status.APPROVED)
        self.assertTrue(any('could not be added back to stock' in m for m in self.flash(response)))

    # -- reject ----------------------------------------------------------------

    def test_a_rejection_needs_a_reason_and_changes_nothing_else(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        refund = Refund.objects.get()
        before = self.stock()

        blank = self.decide_as(self.manager, refund, approve=False, note='  ')
        self.assertTrue(any('reason for rejecting' in m for m in self.flash(blank)))
        self.assertEqual(Refund.objects.get().status, Refund.Status.PENDING)

        self.decide_as(self.manager, refund, approve=False, note='Item was not ours')

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.REJECTED)
        self.assertEqual(refund.decision_note, 'Item was not ours')
        self.assertIsNone(refund.refunded_by)
        self.assertEqual(self.stock(), before)
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('0'))
        self.assertEqual(self.billing_status(txn), 'Paid')
        last = refund.events.order_by('id').last()
        self.assertEqual((last.to_status, last.actor, last.note),
                         ('Rejected', self.manager, 'Item was not ours'))

    def test_a_rejected_refund_is_not_counted_in_reports(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        scope = SalesTransaction.objects.filter(pk=txn.pk)
        self.assertEqual(refund_value_total(scope), Decimal('0'))         # Pending

        refund = Refund.objects.get()
        self.decide_as(self.manager, refund, approve=False, note='No')
        self.assertEqual(refund_value_total(scope), Decimal('0'))         # Rejected

        self.request_as(self.staff, txn, item, 2)
        self.decide_as(self.manager, Refund.objects.exclude(pk=refund.pk).get())
        self.assertEqual(refund_value_total(scope), Decimal('200.00'))    # Approved
        txn.refresh_from_db()
        self.assertEqual(refund_value_total(scope), txn.refunded_amount)

    # -- who may decide, and where -------------------------------------------------

    def test_staff_can_never_decide_a_refund(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        refund = Refund.objects.get()
        before = self.stock()

        approve = self.decide_as(self.staff, refund)
        reject = self.decide_as(self.staff, refund, approve=False, note='nope')

        self.assertTrue(any('Only the Owner or a Branch Manager' in m for m in self.flash(approve)))
        self.assertTrue(any('Only the Owner or a Branch Manager' in m for m in self.flash(reject)))
        self.assertEqual(Refund.objects.get().status, Refund.Status.PENDING)
        self.assertEqual(self.stock(), before)

    def test_a_manager_cannot_decide_another_branchs_refund(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)

        response = self.decide_as(self.other_manager, Refund.objects.get())

        self.assertTrue(any('another branch' in m for m in self.flash(response)))
        self.assertEqual(Refund.objects.get().status, Refund.Status.PENDING)

    def test_deciding_by_get_does_nothing(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        self.client.force_login(self.manager)

        response = self.client.get(reverse('transaction_refund_approve', args=[Refund.objects.get().pk]))

        self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(Refund.objects.get().status, Refund.Status.PENDING)

    # -- voiding an order --------------------------------------------------------

    def test_voiding_an_order_closes_its_pending_refunds(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        self.client.force_login(self.owner)

        self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'cancel_reason': 'Rung up twice'},
        )

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Cancelled')
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.Status.REJECTED)
        self.assertIn('voided', refund.decision_note)

    def test_an_order_with_an_approved_refund_cannot_also_be_voided(self, _notify):
        """_reverse_stock credits back the FULL quantity sold, so voiding after
        a Sellable refund would put those units on the shelf a second time."""
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.owner, txn, item, 2)          # self-approved, restocked
        stock_after_refund = self.stock()

        response = self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'cancel_reason': 'Mistake'},
        )

        self.assertTrue(any('already been refunded' in m for m in self.flash(response)))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, 'Completed')
        self.assertEqual(self.stock(), stock_after_refund)

    # -- order status trail ---------------------------------------------------------

    def test_status_history_starts_at_creation_and_records_every_step(self, _notify):
        txn = SalesTransaction.objects.create(
            branch=self.branch, transaction_type=SalesTransaction.TransactionType.ONLINE,
            total_amount=Decimal('100'), recorded_by=None, customer=self.customer,
        )
        first = txn.status_history.get()
        self.assertEqual((first.from_status, first.to_status, first.actor),
                         ('', 'Pending', self.customer))

        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_set_status', args=[txn.pk, 'Approved']))
        self.client.post(
            reverse('transaction_set_status', args=[txn.pk, 'Cancelled']),
            {'cancel_reason': 'Customer called'},
        )

        rows = list(txn.status_history.values_list('from_status', 'to_status', 'note'))
        self.assertEqual(rows, [
            ('', 'Pending', 'Order created.'),
            ('Pending', 'Approved', ''),
            ('Approved', 'Cancelled', 'Customer called'),
        ])
        self.assertEqual(txn.status_history.last().actor, self.owner)

    def test_a_rejected_transition_leaves_no_history_row(self, _notify):
        txn = self.order()
        self.client.force_login(self.owner)

        self.client.post(reverse('transaction_set_status', args=[txn.pk, 'Preparing']))

        self.assertEqual(txn.status_history.count(), 1)      # just the creation row

    # -- the order page --------------------------------------------------------------

    def test_the_order_page_shows_the_log_history_and_decision_controls(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        refund = Refund.objects.get()
        approve_url = reverse('transaction_refund_approve', args=[refund.pk])

        self.client.force_login(self.manager)
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertContains(page, 'Refund Log')
        self.assertContains(page, 'Status History')
        self.assertContains(page, 'Pending approval')
        self.assertContains(page, approve_url)
        self.assertTrue(page.context['can_decide_refunds'])

        self.client.force_login(self.staff)
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertContains(page, 'Refund Log')
        self.assertNotContains(page, approve_url)
        self.assertContains(page, 'Request refund')

    def test_the_order_page_shows_the_net_after_an_approved_refund(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.owner, txn, item, 2)

        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))

        self.assertContains(page, 'Net after refunds')
        self.assertContains(page, '₱300.00')

    # -- notifications -----------------------------------------------------------------

    def test_a_request_and_its_decision_each_raise_a_notification(self, notify):
        txn = self.order()
        item = txn.items.get()

        self.request_as(self.staff, txn, item, 2)
        self.assertIn('needs approval', notify.call_args.args[2])

        self.decide_as(self.manager, Refund.objects.get())
        self.assertIn('Refund issued', notify.call_args.args[2])


class RefundNotificationDeliveryTests(RefundWorkflowBase, TestCase):
    def setUp(self):
        self.make_users()

    def test_the_branch_manager_is_told_when_staff_ask_for_a_refund(self):
        txn = self.order()
        item = txn.items.get()

        self.request_as(self.staff, txn, item, 2)

        self.assertTrue(
            Notification.objects.filter(user=self.manager, title__startswith='Refund needs approval').exists()
        )
        self.assertFalse(
            Notification.objects.filter(user=self.other_manager, title__startswith='Refund needs approval').exists()
        )


@mock.patch('core.notifications.notify')
class RefundApiTests(RefundWorkflowBase, TestCase):
    def setUp(self):
        self.make_users()

    def post(self, user, url, data):
        self.client.force_login(user)
        return self.client.post(url, data=json.dumps(data), content_type='application/json')

    def body(self, item, quantity=2, **extra):
        return {
            'reasonCode': 'Defective', 'refundMethod': 'Cash',
            'items': [{'itemId': item.pk, 'quantity': quantity, 'condition': 'Sellable'}],
            **extra,
        }

    def test_staff_request_is_pending_over_the_api(self, _notify):
        txn = self.order()
        item = txn.items.get()

        response = self.post(self.staff, reverse('api-sales-refunds', args=[txn.pk]), self.body(item))

        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual((data['status'], data['refundType'], data['totalAmount']),
                         ('Pending', 'Partial', '200.00'))
        self.assertEqual(data['requestedBy'], 'wf-staff')
        self.assertEqual([e['toStatus'] for e in data['events']], ['Pending'])
        self.assertEqual(self.stock(), 50)

    def test_a_managers_request_is_approved_as_it_is_sent(self, _notify):
        txn = self.order()
        item = txn.items.get()

        response = self.post(self.manager, reverse('api-sales-refunds', args=[txn.pk]), self.body(item))

        self.assertEqual(response.json()['status'], 'Approved')
        self.assertEqual(self.stock(), 52)
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('200.00'))

    def test_full_refund_needs_a_condition_and_covers_every_unit(self, _notify):
        txn = self.order()
        url = reverse('api-sales-refunds', args=[txn.pk])
        base = {'reasonCode': 'Wrong Item', 'refundMethod': 'Cash', 'fullRefund': True}

        missing = self.post(self.owner, url, base)
        self.assertEqual(missing.status_code, 400)
        self.assertIn('condition', missing.json())

        ok = self.post(self.owner, url, {**base, 'condition': 'Damaged'})
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()['refundType'], 'Full')
        self.assertEqual(self.billing_status(txn), 'Refunded')

    def test_bad_input_is_a_400_with_a_message(self, _notify):
        txn = self.order()
        item = txn.items.get()
        url = reverse('api-sales-refunds', args=[txn.pk])

        too_many = self.post(self.staff, url, self.body(item, quantity=9))
        self.assertEqual(too_many.status_code, 400)
        self.assertIn('Only 5 unit', too_many.json()['detail'])

        other_no_note = self.post(self.staff, url, {**self.body(item), 'reasonCode': 'Other'})
        self.assertEqual(other_no_note.status_code, 400)

        bogus = self.post(self.staff, url, {**self.body(item), 'reasonCode': 'Nope'})
        self.assertEqual(bogus.status_code, 400)
        self.assertEqual(Refund.objects.count(), 0)

    def test_only_owner_or_manager_can_decide(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        refund = Refund.objects.get()
        approve = reverse('api-sales-refund-approve', args=[refund.pk])
        reject = reverse('api-sales-refund-reject', args=[refund.pk])

        self.assertEqual(self.post(self.staff, approve, {}).status_code, 403)
        self.assertEqual(self.post(self.customer, approve, {}).status_code, 403)
        self.assertEqual(self.post(self.other_manager, approve, {}).status_code, 404)
        self.assertEqual(Refund.objects.get().status, 'Pending')

        no_note = self.post(self.manager, reject, {})
        self.assertEqual(no_note.status_code, 400)

        done = self.post(self.manager, reject, {'note': 'Not eligible'})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()['status'], 'Rejected')
        self.assertEqual(done.json()['decisionNote'], 'Not eligible')

        again = self.post(self.manager, approve, {})
        self.assertEqual(again.status_code, 400)
        self.assertIn('already rejected', again.json()['detail'])

    def test_a_manager_approves_over_the_api(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.staff, txn, item, 2)
        refund = Refund.objects.get()

        response = self.post(
            self.manager, reverse('api-sales-refund-approve', args=[refund.pk]), {'note': 'ok'}
        )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual((data['status'], data['approvedBy']), ('Approved', 'wf-manager'))
        self.assertEqual([e['toStatus'] for e in data['events']], ['Pending', 'Approved'])
        self.assertEqual(self.stock(), 52)

    def test_branch_scoping_on_the_refund_endpoints(self, _notify):
        txn = self.order()
        item = txn.items.get()
        url = reverse('api-sales-refunds', args=[txn.pk])

        self.client.force_login(self.other_manager)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.post(self.other_staff, url, self.body(item)).status_code, 404)
        self.assertEqual(self.post(self.customer, url, self.body(item)).status_code, 403)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(Refund.objects.count(), 0)

    def test_the_order_endpoint_exposes_totals_history_and_the_refund_log(self, _notify):
        txn = self.order()
        item = txn.items.get()
        self.request_as(self.owner, txn, item, 2)

        self.client.force_login(self.manager)
        data = self.client.get(reverse('api-sales-detail', args=[txn.pk])).json()

        self.assertEqual(data['totalAmount'], '500.00')
        self.assertEqual(data['refundedAmount'], '200.00')
        self.assertEqual(data['netAmount'], '300.00')
        self.assertEqual(data['paymentStatus'], 'Partially Refunded')
        self.assertEqual([h['toStatus'] for h in data['statusHistory']], ['Completed'])
        self.assertEqual(len(data['refunds']), 1)
        self.assertEqual(data['refunds'][0]['items'][0]['productName'], 'Rice (Sack)')

    def test_the_refund_log_and_history_are_not_served_to_the_customer(self, _notify):
        txn = self.order(customer=self.customer)
        item = txn.items.get()
        self.request_as(self.owner, txn, item, 2)
        txn.refresh_from_db()

        as_customer = SalesTransactionSerializer(
            txn, context={'request': SimpleNamespace(user=self.customer)}
        ).data
        no_context = SalesTransactionSerializer(txn).data
        as_staff = SalesTransactionSerializer(
            txn, context={'request': SimpleNamespace(user=self.staff)}
        ).data

        self.assertEqual(as_customer['netAmount'], '300.00')       # their own money is fine
        for data in (as_customer, no_context):                       # ...internal detail is not
            self.assertNotIn('refunds', data)
            self.assertNotIn('statusHistory', data)
        self.assertIn('refunds', as_staff)

    def test_listing_orders_does_not_query_per_order(self, _notify):
        first = self.order()
        self.request_as(self.owner, first, first.items.get(), 1)
        self.client.force_login(self.owner)
        url = reverse('api-sales-list')

        with CaptureQueriesContext(connection) as one:
            self.client.get(url)
        for _ in range(3):
            extra = self.order()
            self.request_as(self.owner, extra, extra.items.get(), 1)
        self.client.force_login(self.owner)
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(url)

        self.assertEqual(len(response.json()), 4)
        self.assertEqual(len(one), len(many))


class LegacyRefundCarryOverTests(RefundWorkflowBase, TestCase):
    """Refunds issued before P14 were instant; the migration carries them over
    as Approved, requested and approved by whoever issued them."""

    def setUp(self):
        self.make_users()

    def test_existing_refunds_are_carried_over_as_approved(self):
        migration = importlib.import_module('sales.migrations.0016_refund_approval_workflow')
        txn = self.order(quantity=4)
        item = txn.items.get()
        issued = timezone.now() - timedelta(days=1)
        legacy = Refund.objects.create(
            transaction=txn, status=Refund.Status.APPROVED, reason_code='Other',
            reason='old', refund_type='Partial', refund_method='Cash',
            total_amount=Decimal('400.00'), refunded_by=self.manager, refunded_at=issued,
        )
        RefundItem.objects.create(
            refund=legacy, sales_item=item, quantity=4, condition='Damaged', amount=Decimal('400.00'),
        )
        SalesTransaction.objects.filter(pk=txn.pk).update(refunded_amount=0)

        migration.carry_over_existing_refunds(django_apps, None)

        legacy.refresh_from_db()
        self.assertEqual(legacy.requested_by, self.manager)
        self.assertEqual(legacy.requested_at, issued)
        self.assertEqual(legacy.refund_type, 'Full')
        event = legacy.events.get()
        self.assertEqual((event.to_status, event.actor, event.created_at), ('Approved', self.manager, issued))
        txn.refresh_from_db()
        self.assertEqual(txn.refunded_amount, Decimal('400.00'))


class ApprovalRaceTests(RefundWorkflowBase, TransactionTestCase):
    """Two people approving the same Pending refund at the same moment must
    restock and record it exactly once. Needs real row locks, so MySQL only."""

    def setUp(self):
        self.make_users()

    def _approve(self, refund_id, actor, results, index, barrier):
        try:
            barrier.wait(timeout=10)
            with mock.patch('core.notifications.notify'):
                refund_service.approve_refund(refund_id, actor)
            results[index] = 'approved'
        except refund_service.RefundError as e:
            results[index] = f'refused: {e}'
        except Exception as e:  # recorded, re-asserted on the main thread
            results[index] = f'error: {e!r}'
        finally:
            connections.close_all()

    def test_two_simultaneous_approvals_apply_once(self):
        if connections['default'].vendor == 'sqlite':
            self.skipTest('sqlite takes no row lock for SELECT ... FOR UPDATE.')

        for _ in range(3):
            txn = self.order(quantity=5)
            item = txn.items.get()
            before = self.stock()
            with mock.patch('core.notifications.notify'):
                refund = refund_service.request_refund(
                    txn.pk, self.staff, reason_code='Defective', reason='', refund_method='Cash',
                    lines=[{'item_id': item.pk, 'quantity': 3, 'condition': 'Sellable'}],
                ).refund
            results = [None, None]
            barrier = threading.Barrier(2)
            threads = [
                threading.Thread(target=self._approve, args=(refund.pk, actor, results, i, barrier))
                for i, actor in enumerate((self.owner, self.manager))
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            self.assertEqual(sorted(r.split(':')[0] for r in results), ['approved', 'refused'], results)
            self.assertEqual(self.stock(), before + 3)
            txn.refresh_from_db()
            self.assertEqual(txn.refunded_amount, Decimal('300.00'))
            self.assertEqual(Refund.objects.get(pk=refund.pk).events.filter(to_status='Approved').count(), 1)
