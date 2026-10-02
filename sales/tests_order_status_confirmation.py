"""Order-status confirmation (P12).

Completing or cancelling an order stops for a dialog on Order Detail that
shows the order, what the step does, and takes a reason. The dialog is only
the UI half — these tests pin down the server half: which requests are
refused, what a reason is stored as, and that the page draws the dialog only
for the steps that warrant one.

There is no DRF endpoint for order status changes (sales.api_urls only lists
and creates), so the Order Detail form is the only path to cover.
"""
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, StaffProfile, User
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import OrderStatusChange, SalesItem, SalesTransaction
from sales.views import STATUS_REASON_MAX_LENGTH

Status = SalesTransaction.OrderStatus


class _OrderStatusBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='osc-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.other_branch = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-OSC', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.client.force_login(self.owner)

    def _order(self, status=Status.PENDING, online=True, qty=2, paid=False):
        txn = SalesTransaction.objects.create(
            branch=self.branch, order_status=status, total_amount=100 * qty,
            transaction_type=(
                SalesTransaction.TransactionType.ONLINE if online
                else SalesTransaction.TransactionType.PHYSICAL
            ),
            mode_of_payment=SalesTransaction.ModeOfPayment.COD,
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=qty, unit_price=100,
        )
        if paid:
            BillingRecord.objects.filter(transaction=txn).update(
                status=BillingRecord.PaymentStatus.PAID
            )
        return txn

    def _post(self, txn, target, data=None):
        return self.client.post(
            reverse('transaction_set_status', args=[txn.pk, target]), data or {}
        )

    def _messages(self, response):
        return [m.message for m in response.wsgi_request._messages]


class ConfirmationDialogRenderingTests(_OrderStatusBase):
    def _page(self, txn):
        return self.client.get(reverse('transaction_detail', args=[txn.pk]))

    def test_cancel_dialog_shows_summary_target_impact_and_required_reason(self):
        txn = self._order(Status.PENDING, online=True, qty=3)
        page = self._page(txn)
        html = page.content.decode()

        self.assertIn('data-status-confirm', html)
        self.assertIn('Cancel this order?', html)
        # summary
        self.assertIn(txn.order_number, html)
        self.assertIn('Tarcan', html)
        self.assertIn('3 units', html)
        self.assertIn('300.00', html)
        # impact notice specific to an online order that never touched stock
        self.assertIn('No stock has been deducted for this order yet', html)
        # reason box, required, and the state the page was drawn in
        self.assertIn('name="reason"', html)
        self.assertIn('data-reason-required', html)
        self.assertIn('name="expected_status" value="Pending"', html)

    def test_completed_dialog_has_an_optional_reason_and_stock_notice(self):
        txn = self._order(Status.PREPARING, online=True, paid=True)
        html = self._page(txn).content.decode()

        self.assertIn('Mark this order as Completed?', html)
        self.assertIn('Stock for this order is deducted from Tarcan.', html)
        self.assertIn('refund window', html)
        completed_dialog = html.split('Mark this order as Completed?')[1]
        self.assertNotIn('data-reason-required', completed_dialog.split('data-status-ok')[0])

    def test_void_of_a_completed_sale_says_so(self):
        txn = self._order(Status.COMPLETED, online=True, paid=True)
        html = self._page(txn).content.decode()

        self.assertIn('Void this completed sale?', html)
        self.assertIn('the stock it took from Tarcan is put back', html)

    def test_routine_steps_get_no_dialog(self):
        # Pending online: Approved (routine) and Cancelled (dialog).
        txn = self._order(Status.PENDING, online=True)
        page = self._page(txn)
        html = page.content.decode()

        self.assertEqual(page.context['next_actions'], ['Approved', 'Cancelled'])
        # Scoped to the per-step forms themselves (.od-move), not the whole
        # page: the Pending billing record billing.signals auto-creates for
        # this order draws its own Reject Payment dialog on the same page
        # (Financials UX Audit Phase 1), which is a second, unrelated use of
        # this same generic data-status-confirm hook — counting the whole
        # page would conflate the two.
        move_html = html.split('<div class="od-move">')[1].split('<div class="card')[0]
        self.assertEqual(move_html.count('data-status-confirm'), 1)
        approved = [i for i in page.context['next_action_items'] if i['status'] == 'Approved']
        self.assertIsNone(approved[0]['confirm'])


class StatusTransitionServerValidationTests(_OrderStatusBase):
    def _assert_untouched(self, txn, status):
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, status)
        self.assertEqual(OrderStatusChange.objects.filter(transaction=txn).count(), 0)

    def test_invalid_transitions_are_refused_without_a_history_row(self):
        txn = self._order(Status.PENDING, online=True, paid=True)
        for target in ('Completed', 'Preparing', 'Pending', 'Refund Requested', 'Bogus'):
            response = self._post(txn, target, {'reason': 'x'})
            self.assertRedirects(response, reverse('transaction_detail', args=[txn.pk]))
            self.assertTrue(
                any('Cannot move' in m for m in self._messages(response)), target
            )
        self._assert_untouched(txn, Status.PENDING)

    def test_a_cancelled_order_cannot_move_again(self):
        txn = self._order(Status.CANCELLED)
        self._post(txn, 'Approved')
        self._post(txn, 'Completed')
        self._post(txn, 'Cancelled', {'reason': 'again'})
        self._assert_untouched(txn, Status.CANCELLED)

    def test_get_does_not_change_status(self):
        txn = self._order(Status.PENDING)
        self.client.get(reverse('transaction_set_status', args=[txn.pk, 'Cancelled']))
        self._assert_untouched(txn, Status.PENDING)

    def test_cancelling_requires_a_reason_and_leaves_no_trace_without_one(self):
        txn = self._order(Status.PENDING)
        response = self._post(txn, 'Cancelled', {'reason': '   '})
        self.assertTrue(any('reason' in m for m in self._messages(response)))
        self._assert_untouched(txn, Status.PENDING)

    def test_overlong_reason_is_refused_and_nothing_moves(self):
        txn = self._order(Status.PREPARING, online=True, paid=True)
        response = self._post(txn, 'Completed', {'reason': 'x' * (STATUS_REASON_MAX_LENGTH + 1)})
        self.assertTrue(any('characters' in m for m in self._messages(response)))
        self._assert_untouched(txn, Status.PREPARING)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)

    def test_completing_is_still_gated_on_paid_payment(self):
        txn = self._order(Status.PREPARING, online=True, paid=False)
        response = self._post(txn, 'Completed', {'reason': 'ok'})
        self.assertTrue(any('verified as Paid' in m for m in self._messages(response)))
        self._assert_untouched(txn, Status.PREPARING)

    def test_stale_expected_status_is_refused(self):
        txn = self._order(Status.PENDING)
        # Someone else cancels it after this person's page was drawn.
        SalesTransaction.objects.filter(pk=txn.pk).update(order_status=Status.APPROVED)
        response = self._post(
            txn, 'Cancelled', {'reason': 'x', 'expected_status': 'Pending'}
        )
        self.assertTrue(any('changed while you were reviewing' in m for m in self._messages(response)))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.APPROVED)
        self.assertEqual(OrderStatusChange.objects.count(), 0)

    def test_matching_expected_status_proceeds(self):
        txn = self._order(Status.PENDING)
        with mock.patch('core.notifications.notify'):
            self._post(txn, 'Cancelled', {'reason': 'x', 'expected_status': 'Pending'})
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.CANCELLED)

    def test_insufficient_stock_rolls_back_status_and_history(self):
        txn = self._order(Status.PREPARING, online=True, qty=99, paid=True)
        response = self._post(txn, 'Completed', {'reason': 'ship it'})
        self.assertTrue(any('Not enough stock' in m for m in self._messages(response)))
        self._assert_untouched(txn, Status.PREPARING)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)


class StatusChangeRecordingTests(_OrderStatusBase):
    @mock.patch('core.notifications.notify')
    def test_cancel_reason_is_stored_on_order_and_in_history(self, _notify):
        txn = self._order(Status.PENDING)
        self._post(txn, 'Cancelled', {'reason': 'Customer changed their mind'})

        txn.refresh_from_db()
        self.assertEqual(txn.cancel_reason, 'Customer changed their mind')
        change = OrderStatusChange.objects.get(transaction=txn)
        self.assertEqual(
            (change.from_status, change.to_status, change.reason, change.changed_by),
            ('Pending', 'Cancelled', 'Customer changed their mind', self.owner),
        )

    @mock.patch('core.notifications.notify')
    def test_legacy_cancel_reason_field_is_still_accepted(self, _notify):
        txn = self._order(Status.PENDING)
        self._post(txn, 'Cancelled', {'cancel_reason': 'old client'})
        txn.refresh_from_db()
        self.assertEqual(txn.cancel_reason, 'old client')

    def test_completed_records_optional_note_and_deducts_stock_once(self):
        txn = self._order(Status.PREPARING, online=True, qty=2, paid=True)
        self._post(txn, 'Completed', {'reason': 'Handed to rider'})
        # A double-submit of the same confirmation must not deduct twice.
        self._post(txn, 'Completed', {'reason': 'Handed to rider'})

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.COMPLETED)
        self.assertIsNotNone(txn.completed_at)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 48)
        changes = OrderStatusChange.objects.filter(transaction=txn)
        self.assertEqual(changes.count(), 1)
        self.assertEqual(changes[0].reason, 'Handed to rider')

    def test_completed_without_a_note_is_fine(self):
        txn = self._order(Status.PREPARING, online=True, paid=True)
        self._post(txn, 'Completed')
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.COMPLETED)
        self.assertEqual(OrderStatusChange.objects.get(transaction=txn).reason, '')

    def test_history_is_shown_on_the_detail_page(self):
        txn = self._order(Status.PREPARING, online=True, paid=True)
        self._post(txn, 'Completed', {'reason': 'Handed to rider'})
        html = self.client.get(reverse('transaction_detail', args=[txn.pk])).content.decode()
        self.assertIn('Status History', html)
        self.assertIn('Handed to rider', html)

    @mock.patch('core.notifications.notify')
    def test_voiding_a_completed_sale_reverses_stock_and_records_it(self, _notify):
        txn = self._order(Status.PREPARING, online=True, qty=2, paid=True)
        self._post(txn, 'Completed')
        self._post(txn, 'Cancelled', {'reason': 'Wrong goods'})

        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.CANCELLED)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 50)
        self.assertEqual(
            list(OrderStatusChange.objects.filter(transaction=txn).values_list('to_status', flat=True)),
            ['Completed', 'Cancelled'],
        )


class StatusChangeAuthorizationTests(_OrderStatusBase):
    def _staff(self, branch, username='osc-staff'):
        staff = User.objects.create_user(username=username, password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'staff_name': username, 'branch': branch}
        )
        return staff

    def test_staff_cannot_void_a_completed_sale(self):
        txn = self._order(Status.COMPLETED, online=True, paid=True)
        self.client.force_login(self._staff(self.branch))
        response = self._post(txn, 'Cancelled', {'reason': 'nope'})
        self.assertTrue(any('Cannot move' in m for m in self._messages(response)))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.COMPLETED)
        self.assertEqual(OrderStatusChange.objects.count(), 0)
        # ...and the dialog for it isn't offered to them either.
        page = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertNotIn('data-status-confirm', page.content.decode())

    def test_staff_of_another_branch_cannot_change_status(self):
        txn = self._order(Status.PENDING)
        self.client.force_login(self._staff(self.other_branch))
        response = self._post(txn, 'Cancelled', {'reason': 'x'})
        self.assertRedirects(response, reverse('transaction_list'))
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.PENDING)
        self.assertEqual(OrderStatusChange.objects.count(), 0)

    def test_manager_of_another_branch_cannot_change_status(self):
        txn = self._order(Status.PENDING)
        manager = User.objects.create_user(
            username='osc-mgr', password='pw', role=User.Role.BRANCH_MANAGER
        )
        BranchManagerProfile.objects.update_or_create(
            user=manager, defaults={'manager_name': 'M', 'branch': self.other_branch}
        )
        self.client.force_login(manager)
        self._post(txn, 'Cancelled', {'reason': 'x'})
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.PENDING)

    def test_customer_cannot_change_status(self):
        txn = self._order(Status.PENDING)
        customer = User.objects.create_user(
            username='osc-cust', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(customer)
        self._post(txn, 'Cancelled', {'reason': 'x'})
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.PENDING)

    def test_anonymous_cannot_change_status(self):
        txn = self._order(Status.PENDING)
        self.client.logout()
        self._post(txn, 'Cancelled', {'reason': 'x'})
        txn.refresh_from_db()
        self.assertEqual(txn.order_status, Status.PENDING)
