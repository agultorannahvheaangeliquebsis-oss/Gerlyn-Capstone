"""Billing Detail, state by state — Financials UX Audit Phase 3.

The page was one fixed layout (a stepper it could not move, then a card
headed "Verify this payment") with the state differences buried in table
rows, so a bill missing its reference number, a bill the reader is barred
from verifying themselves, and a bill ready to verify all opened the same
way. It now leads with a Next Step block that draws ONLY the actions
billing_confirm / billing_create would accept for that state.

Nothing here tests a business rule — every rule these checks lean on
(ALLOWED_STATUS_TRANSITIONS, is_self_confirmation, pwd_review_block_message,
missing_payment_evidence, record_payment_details' Pending-only write) is
already covered in billing/tests.py against the views themselves. These are
UI-contract tests: they pin that the page never OFFERS a step the server
would refuse, and never mutates what belongs to Sales & Orders.
"""
import shutil
import tempfile
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffProfile, User
from billing.models import BillingRecord
from billing.tests import _valid_png_file
from branches.models import Branch
from core.module_permissions import (
    BILLING, DEFAULT_STAFF_MODULES, default_module_actions,
)
from inventory.models import Product
from sales.models import (
    Refund, RefundEvent, RefundItem, SalesItem, SalesTransaction,
)


class BillingDetailStateTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # proof_of_payment writes through PaymentProofStorage, whose location
        # is resolved once and cached — same scratch-dir dance the other
        # proof tests do, so nothing lands in the real media tree.
        storage = BillingRecord._meta.get_field('proof_of_payment').storage
        scratch = tempfile.mkdtemp(prefix='billing-detail-states-')
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

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='bds-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='BDS Branch', location='Baliuag', owner=cls.owner
        )
        cls.customer = User.objects.create_user(
            username='bds-customer', password='pw', role=User.Role.CUSTOMER,
            first_name='Dana', last_name='Cruz',
        )
        cls.staff = User.objects.create_user(
            username='bds-staff', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff, defaults={'staff_name': 'Sam', 'branch': cls.branch}
        )
        # A new Staff account has no Billing module, and
        # ModulePermissionMiddleware would turn it away before billing_detail
        # ran — see core.module_permissions.
        User.objects.filter(pk=cls.staff.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
        )
        cls.staff.refresh_from_db()
        cls.product = Product.objects.create(
            product_name='BDS Widget', sku_code='BDS-1', unit_price=Decimal('125.00'),
        )

    # -- fixtures ---------------------------------------------------------

    def _txn(self, staff=None, total='250.00', **kwargs):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=staff, customer=self.customer,
            total_amount=Decimal(total), **kwargs
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=2,
            unit_price=Decimal('125.00'), subtotal=Decimal('250.00'),
        )
        return txn

    def _record(self, status=BillingRecord.PaymentStatus.PENDING,
                method=BillingRecord.PaymentMethod.CASH, reference='',
                proof=False, staff=None, reject_reason='', confirmed=False,
                **txn_kwargs):
        """The auto-created billing record for a fresh sale, moved onto the
        state under test. Written straight onto the row rather than through
        billing_confirm on purpose: these are rendering checks, and the
        transitions themselves are covered in billing/tests.py.
        """
        txn = self._txn(staff=staff, **txn_kwargs)
        record = txn.billing_record
        record.payment_method = method
        record.reference_number = reference
        record.reject_reason = reject_reason
        record.status = status
        if proof:
            record.proof_of_payment.save('proof.png', _valid_png_file(), save=False)
        if confirmed or status in (
            BillingRecord.PaymentStatus.PAID,
            BillingRecord.PaymentStatus.REJECTED,
            BillingRecord.PaymentStatus.PARTIALLY_REFUNDED,
            BillingRecord.PaymentStatus.REFUNDED,
        ):
            record.confirmed_by = self.owner
            record.date_confirmed = timezone.now()
        record.save()
        return record

    def _html(self, record, user=None, query=''):
        self.client.force_login(user or self.owner)
        response = self.client.get(
            reverse('billing_detail', args=[record.pk]) + query
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _assert_links_to_canonical_order(self, record, html):
        """Phase 4 D1: View Order is the plain Order Detail URL. It used to
        carry ?from=billing, a flag from the old stacked-overlay design that
        makes Order Detail drop its Payment Status card — wrong once the
        order is reached by ordinary navigation."""
        order_url = reverse('transaction_detail', args=[record.transaction.pk])
        self.assertIn(f'href="{order_url}"', html)
        self.assertNotIn(f'{order_url}?from=billing', html)

    @staticmethod
    def _next_step(html):
        """Just the Next Step notice: from its <section> to the payment
        block (the page's <style> comments are left out)."""
        notice = html.partition('<section class="card card-pad bill-next')[2]
        return notice.partition('<div class="bill-pay">')[0]

    # -- Pending: awaiting payment details --------------------------------

    def test_pending_missing_evidence_asks_for_details_and_hides_verify(self):
        record = self._record(method=BillingRecord.PaymentMethod.GCASH)
        html = self._html(record)

        self.assertIn('Waiting for the payment details', html)
        self.assertIn('reference number and proof of payment', html)
        # Edit is the step the reader is actually on, so it is the primary.
        self.assertIn(
            '<a class="btn btn-primary" href="/billing/create/'
            f'{record.transaction.pk}/?from=billing"', html
        )
        self.assertNotIn('Verify Payment', html)
        self.assertIn('Reject Payment', html)

    # -- Pending: ready to verify -----------------------------------------

    def test_pending_ready_offers_verify_reject_and_edit(self):
        record = self._record()
        html = self._html(record)

        self.assertIn('Verify this payment', html)
        self.assertIn(
            '<button type="submit" name="status" value="Paid" class="btn btn-primary">Verify Payment</button>',
            html,
        )
        self.assertIn('Reject Payment', html)
        self.assertIn('Edit payment details', html)

    def test_reject_form_posts_the_status_itself(self):
        """The confirm dialog's own Reject button carries no name/value, so
        the form has to send status=Rejected itself. Without it the dialog
        submits nothing but `reason`, and billing_confirm refuses it as
        "Invalid payment status" — the rejection silently never happens."""
        record = self._record()
        form = self._html(record).partition('data-status-confirm')[2]
        form = form.partition('</form>')[0]
        self.assertIn('<input type="hidden" name="status" value="Rejected">', form)

    def test_pending_ready_folds_edit_and_reject_into_a_more_menu(self):
        """Verify Payment stays the one visible button; Edit payment details
        and Reject Payment sit behind "More". The Reject row submits the
        reject form by id, so the form (and its confirm dialog) must be on
        the page outside the menu, and must post status=Rejected on its own
        — the dialog's confirming button has no name/value to send it."""
        record = self._record()
        notice = self._next_step(self._html(record))

        self.assertIn('<details class="bill-more">', notice)
        menu = notice.partition('<details class="bill-more">')[2]
        menu = menu.partition('</details>')[0]
        self.assertIn('Edit payment details', menu)
        self.assertIn('form="bill-reject-form" name="status" value="Rejected"', menu)
        self.assertNotIn('<form', menu)

        reject_form = notice.partition('id="bill-reject-form"')[2]
        reject_form = reject_form.partition('</form>')[0]
        self.assertIn('<input type="hidden" name="status" value="Rejected">', reject_form)

    def test_ready_to_verify_drops_the_stripe_but_keeps_what_it_said(self):
        """The green left edge was a third telling of "this is ready" — the
        heading, the method-specific instruction and the Verify button all
        say it. The stripe goes; the states that flag a blocker or an
        outcome keep theirs."""
        html = self._html(self._record())

        self.assertIn('.bill-next--action { border-left: 0; }', html)
        self.assertIn('bill-next--action', html)
        self.assertIn('Verify this payment', html)
        self.assertIn('Verify Payment', html)
        self.assertIn('.bill-next--wait  { border-left-color: var(--warn-fg); }', html)
        self.assertIn('.bill-next--danger { border-left-color: var(--danger-fg); }', html)

    def test_more_menu_and_reject_form_need_the_billing_edit_permission(self):
        """Both are write controls, so neither is drawn without the grant —
        the reject form moved out of the actions row and must not escape it."""
        record = self._record()
        User.objects.filter(pk=self.staff.pk).update(
            module_actions={**default_module_actions(), BILLING: []}
        )
        notice = self._next_step(self._html(record, user=self.staff))

        self.assertNotIn('bill-more', notice)
        self.assertNotIn('id="bill-reject-form"', notice)
        self.assertNotIn('Reject Payment', notice)

    def test_view_order_is_offered_once_not_in_the_header_too(self):
        """The header used to carry its own View Order beside the one on the
        Order Information card — the same destination twice on one screen."""
        record = self._record()
        html = self._html(record)
        order_url = reverse('transaction_detail', args=[record.transaction.pk])
        self.assertEqual(html.count(f'href="{order_url}"'), 1)

    def test_pending_ready_instruction_is_method_specific(self):
        cash = self._record(method=BillingRecord.PaymentMethod.CASH)
        self.assertIn('Count ₱250.00 at the counter', self._html(cash))

        gcash = self._record(
            method=BillingRecord.PaymentMethod.GCASH,
            reference='GC-2026-0007', proof=True,
        )
        self.assertIn('Check reference GC-2026-0007', self._html(gcash))

    # -- Pending: recorded by the reader (A-1) ----------------------------

    def test_pending_self_recorded_offers_neither_verify_nor_reject(self):
        """A-1 maker/checker, said out loud instead of after the click.

        billing_confirm refuses BOTH Paid and Rejected from the staff member
        who rang up the sale (is_self_confirmation gates every decision that
        records a confirmer), so neither button is drawn for them. Editing
        the details they recorded is still theirs.
        """
        record = self._record(staff=self.staff)
        html = self._html(record, user=self.staff)

        self.assertIn(
            'You recorded this payment. Another staff member, a manager, '
            'or the owner has to verify it.', html
        )
        self.assertNotIn('Verify Payment', html)
        self.assertNotIn('Reject Payment', html)
        self.assertIn('Edit payment details', html)

    def test_the_same_record_offers_verify_to_a_different_verifier(self):
        """The block is about WHO is reading, not about the record — the
        Owner opening the same bill still gets the decision."""
        record = self._record(staff=self.staff)
        html = self._html(record, user=self.owner)

        self.assertIn('Verify Payment', html)
        self.assertNotIn('You recorded this payment', html)

    # -- Pending: blocked by PWD review -----------------------------------

    def test_pending_pwd_request_blocks_verify_and_points_at_the_order(self):
        """P15, preserved: the amount due is not final until the PWD ID has
        been checked, and that review lives on the order — Billing neither
        performs it nor invents a workflow for it."""
        record = self._record(
            pwd_request_status=SalesTransaction.PwdRequestStatus.PENDING,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            pwd_requested_at=timezone.now(),
        )
        html = self._html(record)

        self.assertIn('PWD discount review comes first', html)
        self.assertIn(
            '<button type="button" class="btn btn-primary" disabled>Verify Payment</button>',
            html,
        )
        # Rejecting a payment settles nothing, so it is still allowed.
        self.assertIn('Reject Payment', html)
        self._assert_links_to_canonical_order(record, html)
        # The review itself is not offered here.
        self.assertNotIn('pwd_request_review', html)

    # -- Paid --------------------------------------------------------------

    def test_paid_states_who_verified_it_and_offers_no_payment_action(self):
        record = self._record(status=BillingRecord.PaymentStatus.PAID)
        html = self._html(record)

        self.assertIn('Payment verified', html)
        self.assertIn('bds-owner', html)
        self.assertIn('Verified on', html)
        self.assertNotIn('Verify Payment', html)
        self.assertNotIn('Reject Payment', html)
        self.assertNotIn('Re-open Payment', html)
        self.assertNotIn('Edit payment details', html)
        self.assertNotIn('id="record-payment-modal"', html)

    def test_paid_but_unfinished_order_links_to_the_order(self):
        # A walk-in still Pending: sales.views._next_actions offers Completed.
        record = self._record(status=BillingRecord.PaymentStatus.PAID)
        html = self._html(record)
        self.assertIn('The order can now be marked Completed', html)
        self._assert_links_to_canonical_order(record, html)

    def test_paid_online_order_in_preparing_can_be_confirmed_as_handed_over(self):
        """D3 — an online order is completed by confirming its delivery or
        pickup (sales.receipts), not by a plain status step, so that is what
        the notice points at once the payment is verified."""
        record = self._record(
            status=BillingRecord.PaymentStatus.PAID,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PREPARING,
        )
        notice = self._next_step(self._html(record))
        self.assertIn('The delivery or pickup can now be confirmed', notice)
        self.assertNotIn('can now be marked Completed', notice)

    def test_paid_online_order_not_yet_preparing_does_not_claim_completion(self):
        """D3 — an online order goes Pending -> Approved -> Preparing ->
        Completed, so from Pending or Approved "can now be marked Completed"
        is false: transaction_set_status would refuse it. The notice says
        where the order stands instead, and invents no action."""
        for order_status in (
            SalesTransaction.OrderStatus.PENDING,
            SalesTransaction.OrderStatus.APPROVED,
        ):
            with self.subTest(order_status=order_status):
                record = self._record(
                    status=BillingRecord.PaymentStatus.PAID,
                    transaction_type=SalesTransaction.TransactionType.ONLINE,
                    order_status=order_status,
                )
                notice = self._next_step(self._html(record))
                self.assertNotIn('can now be marked Completed', notice)
                # Neutral copy, no status name: the page's pill already shows
                # the state in its own terminology ("Awaiting Fulfillment").
                self.assertIn(
                    'Any further steps for the order are taken on the order itself.',
                    notice,
                )

    def test_paid_with_no_date_confirmed_leaves_no_blank_or_stray_period(self):
        """D4 — the POS cash path saves Paid with confirmed_by but no
        date_confirmed (sales.views.transaction_add). The notice says only
        what is on file: no "Verified by bds-owner ." with a dangling
        period, and no invented timestamp."""
        record = self._record(status=BillingRecord.PaymentStatus.PAID)
        record.date_confirmed = None
        record.save(update_fields=['date_confirmed'])
        notice = self._next_step(self._html(record))

        self.assertIn('Verified by bds-owner.', notice)
        self.assertNotRegex(notice, r'bds-owner\s+\.')
        self.assertNotRegex(notice, r'Verified by\s+—')
        self.assertNotIn('Verified by bds-owner on', notice)
        self.assertIn('A verified payment is final', notice)

    def test_paid_with_neither_verifier_nor_date_omits_the_sentence(self):
        """D4 — with nothing on file there is no "Verified by — ." either."""
        record = self._record(status=BillingRecord.PaymentStatus.PAID)
        record.confirmed_by = None
        record.date_confirmed = None
        record.save(update_fields=['confirmed_by', 'date_confirmed'])
        notice = self._next_step(self._html(record))

        self.assertNotIn('Verified by', notice)
        self.assertNotIn('Verified on', notice)
        self.assertNotRegex(notice, r'—\s*\.')
        self.assertIn('A verified payment is final', notice)

    def test_paid_with_verifier_and_date_reads_as_one_sentence(self):
        record = self._record(status=BillingRecord.PaymentStatus.PAID)
        notice = self._next_step(self._html(record))
        # "g:i a" renders "p.m." / "a.m.", which closes the sentence itself —
        # no second period after it.
        self.assertRegex(
            notice, r'Verified by bds-owner on \d{1,2} \w{3} \d{4}, \d{1,2}:\d{2} [ap]\.m\.\s'
        )
        self.assertNotRegex(notice, r'[ap]\.m\.\.')

    # -- Rejected ----------------------------------------------------------

    def test_rejected_shows_the_reason_once_and_only_offers_reopen(self):
        record = self._record(
            status=BillingRecord.PaymentStatus.REJECTED,
            method=BillingRecord.PaymentMethod.GCASH,
            reference='GC-BAD-1', proof=True,
            reject_reason='Reference number does not match.',
        )
        html = self._html(record)

        # Stated once where the decision is explained, and NOT repeated as a
        # row of the payment facts table (where it used to appear as well).
        # The Activity log's own entry for that rejection is not a
        # duplicate: it is the note on a logged event, collapsed by default.
        next_step, _, rest = html.partition('<div class="bill-pay">')
        facts, _, _proof = rest.partition('bill-pay-proof')
        self.assertEqual(next_step.count('Reference number does not match.'), 1)
        self.assertNotIn('Reference number does not match.', facts)
        self.assertNotIn('Reject reason', html)
        self.assertIn('Re-open Payment', html)
        # M9 — billing_create accepts a Pending record only, so the route is
        # Re-open -> Pending -> edit. No edit action while Rejected.
        self.assertNotIn('Edit payment details', html)
        self.assertNotIn('id="record-payment-modal"', html)
        self.assertNotIn('Verify Payment', html)

    # -- Cancelled ---------------------------------------------------------

    def test_cancelled_explains_the_void_and_offers_no_billing_action(self):
        record = self._record(
            status=BillingRecord.PaymentStatus.CANCELLED,
            order_status=SalesTransaction.OrderStatus.CANCELLED,
            cancel_reason='Customer cancelled before pick-up.',
        )
        record.voided_by = self.owner
        record.date_voided = timezone.now()
        record.save(update_fields=['voided_by', 'date_voided'])
        html = self._html(record)

        self.assertIn('This bill is closed', html)
        self.assertIn('Voided by', html)
        self.assertIn('Customer cancelled before pick-up.', html)
        for action in (
            'Verify Payment', 'Reject Payment', 'Re-open Payment',
            'Edit payment details', 'Upload proof',
        ):
            self.assertNotIn(action, html)

    # -- Partially Refunded / Refunded -------------------------------------

    def _refund(self, record, amount, status=Refund.Status.APPROVED):
        txn = record.transaction
        refund = Refund.objects.create(
            transaction=txn, status=status,
            reason_code=Refund.ReasonCode.DEFECTIVE,
            refund_type=Refund.RefundType.PARTIAL,
            refund_method=Refund.RefundMethod.CASH,
            total_amount=Decimal(amount),
            requested_by=self.staff,
            refunded_by=self.owner if status == Refund.Status.APPROVED else None,
        )
        RefundItem.objects.create(
            refund=refund, sales_item=txn.items.first(), quantity=1,
            condition=RefundItem.Condition.SELLABLE, amount=Decimal(amount),
        )
        RefundEvent.objects.create(
            refund=refund, to_status=status, actor=self.staff,
            note='Requested at the counter.',
        )
        if status == Refund.Status.APPROVED:
            txn.refunded_amount = Decimal(amount)
            txn.save(update_fields=['refunded_amount'])
        return refund

    def test_partially_refunded_shows_original_refunded_and_remaining(self):
        record = self._record(status=BillingRecord.PaymentStatus.PARTIALLY_REFUNDED)
        self._refund(record, '125.00')
        html = self._html(record)

        self.assertIn('Part of this payment was refunded', html)
        self.assertIn('Original payment', html)
        self.assertIn('Refunded', html)
        self.assertIn('Remaining', html)
        self.assertIn('₱125.00', html)
        self._assert_links_to_canonical_order(record, html)
        # Refunds are decided on the order, never here.
        self.assertNotIn('Approve refund', html)
        self.assertNotIn('refund_decide', html)

    def test_refunded_says_so_and_exposes_no_billing_control(self):
        record = self._record(status=BillingRecord.PaymentStatus.REFUNDED)
        self._refund(record, '250.00')
        html = self._html(record)

        self.assertIn('Fully refunded', html)
        self.assertIn('₱250.00', html)
        for action in (
            'Verify Payment', 'Reject Payment', 'Re-open Payment',
            'Edit payment details',
        ):
            self.assertNotIn(action, html)

    # -- payment proof, all four combinations ------------------------------

    def test_proof_required_and_missing_asks_for_it(self):
        record = self._record(
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1'
        )
        html = self._html(record)
        self.assertIn('Proof of payment is required for GCash', html)
        self.assertIn('Upload proof', html)

    def test_proof_required_and_present_is_shown_with_the_facts(self):
        record = self._record(
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1', proof=True
        )
        html = self._html(record)
        proof_url = reverse('billing_proof', args=[record.pk])
        self.assertIn(f'<img src="{proof_url}"', html)
        self.assertNotIn('Upload proof', html)
        # The proof sits in the payment block, above Order Items — not under
        # them, where it used to be.
        self.assertLess(html.index('bill-pay-proof'), html.index('od-items-block'))

    def test_proof_not_required_and_absent_says_none_is_needed(self):
        record = self._record(method=BillingRecord.PaymentMethod.CASH)
        html = self._html(record)
        self.assertIn('No proof needed', html)
        self.assertIn("Cash doesn't require a screenshot or receipt.", html)
        self.assertNotIn('Upload proof', html)

    def test_proof_not_required_but_present_is_not_silently_dropped(self):
        record = self._record(method=BillingRecord.PaymentMethod.CASH, proof=True)
        html = self._html(record)
        self.assertIn('Attached — not required for Cash.', html)
        self.assertIn(reverse('billing_proof', args=[record.pk]), html)

    def test_an_attached_proof_can_be_downloaded(self):
        """The existing payment_proof_download endpoint, surfaced — same
        gate, same backend behaviour, just reachable from the page."""
        record = self._record(
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1', proof=True
        )
        download = reverse('payment_proof_download', args=[record.pk])
        self.assertIn(f'href="{download}"', self._html(record))

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(download).status_code, 200)

    # -- the Payment card's proof button -------------------------------------

    @staticmethod
    def _payment_head(html):
        """The Payment card's heading row, where the proof button sits."""
        card = html.partition('bill-pay-facts')[2]
        return card.partition('<dl')[0]

    def test_missing_required_proof_puts_upload_in_the_payment_header(self):
        record = self._record(
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1'
        )
        head = self._payment_head(self._html(record))
        self.assertIn('Upload proof', head)
        self.assertIn('bill-proof-cta', head)
        self.assertIn('data-payment-open="record-payment-modal"', head)
        self.assertNotIn('View proof', head)

    def test_proof_on_file_puts_view_proof_in_the_header_and_a_dialog(self):
        record = self._record(
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1', proof=True
        )
        html = self._html(record)
        head = self._payment_head(html)
        self.assertIn('View proof', head)
        self.assertIn('data-payment-open="proof-view-modal"', head)
        self.assertNotIn('Upload proof', head)
        self.assertIn('id="proof-view-modal"', html)
        # Pending: the dialog hands over to Record Payment to replace it.
        self.assertIn('Replace proof', html)

    def test_view_proof_on_a_closed_bill_offers_no_replace(self):
        record = self._record(
            status=BillingRecord.PaymentStatus.PAID,
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1', proof=True,
        )
        html = self._html(record)
        self.assertIn('View proof', self._payment_head(html))
        self.assertIn('id="proof-view-modal"', html)
        self.assertNotIn('Replace proof', html)
        self.assertNotIn('id="record-payment-modal"', html)

    def test_missing_proof_on_a_rejected_bill_offers_no_upload(self):
        record = self._record(
            status=BillingRecord.PaymentStatus.REJECTED,
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1',
            reject_reason='No screenshot.',
        )
        html = self._html(record)
        self.assertIn('Required — not on file yet', html)
        self.assertNotIn('Upload proof', html)
        self.assertNotIn('id="proof-view-modal"', html)

    def test_proof_write_actions_are_behind_the_billing_edit_permission(self):
        """View and Download are reading; Upload/Replace write through
        billing_create, so they follow module_actions.billing.edit — the
        same gate the Next Step card's Edit payment details sits behind."""
        missing = self._record(
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1'
        )
        on_file = self._record(
            method=BillingRecord.PaymentMethod.GCASH, reference='GC-1', proof=True
        )
        User.objects.filter(pk=self.staff.pk).update(
            module_actions={**default_module_actions(), BILLING: []}
        )

        self.assertNotIn('Upload proof', self._html(missing, user=self.staff))
        viewed = self._html(on_file, user=self.staff)
        self.assertIn('View proof', viewed)
        self.assertNotIn('Replace proof', viewed)

    # -- Order Information, read-only --------------------------------------

    def test_order_information_is_read_only(self):
        record = self._record(
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            fulfillment_type=SalesTransaction.FulfillmentType.PICKUP,
            order_status=SalesTransaction.OrderStatus.APPROVED,
        )
        html = self._html(record)

        self.assertIn('Order Information', html)
        self.assertIn(record.transaction.order_number, html)
        self.assertIn('Dana Cruz', html)
        self.assertIn('BDS Branch', html)
        self.assertIn('Online', html)
        self.assertIn('Pickup', html)
        self.assertIn('Approved', html)
        # The order's own status control and the stepper that framed it are
        # not here: Billing writes to BillingRecord only.
        self.assertNotIn('od-status-select', html)
        self.assertNotIn('order-progress-track', html)
        self.assertNotIn('Change status', html)
        self.assertNotIn(
            reverse('transaction_set_status', args=[record.transaction.pk, 'Completed']),
            html,
        )

    # -- View Order navigation ---------------------------------------------

    def test_view_order_is_plain_navigation_not_a_detail_modal(self):
        record = self._record()
        html = self._html(record)

        self.assertIn('View Order', html)
        self._assert_links_to_canonical_order(record, html)
        self.assertNotIn('data-detail-modal', html)

    def test_every_view_order_link_is_the_canonical_order_url(self):
        """D1 — no View Order link on the page (header, Order Information,
        Next Step) carries ?from=billing, in any state that draws one."""
        order_path = '/sales/'
        for status in (
            BillingRecord.PaymentStatus.PENDING,
            BillingRecord.PaymentStatus.PAID,
            BillingRecord.PaymentStatus.CANCELLED,
            BillingRecord.PaymentStatus.PARTIALLY_REFUNDED,
            BillingRecord.PaymentStatus.REFUNDED,
        ):
            with self.subTest(status=status):
                record = self._record(status=status)
                html = self._html(record)
                self._assert_links_to_canonical_order(record, html)
                self.assertNotRegex(html, order_path + r'\d+/\?from=billing')

    def test_view_order_lands_on_order_detail_with_its_payment_status(self):
        """D1 — follow the link Billing actually renders and check the order
        page is its normal self: the Payment Status card that ?from=billing
        used to strip is there, with its link back to this record."""
        record = self._record()
        html = self._html(record)
        order_url = reverse('transaction_detail', args=[record.transaction.pk])
        self.assertIn(f'href="{order_url}"', html)

        response = self.client.get(order_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h3>Payment Status</h3>', html=False)
        self.assertContains(response, reverse('billing_detail', args=[record.pk]))

    def test_view_order_is_absent_inside_the_billing_record_modal(self):
        """Billing Detail is also read as an overlay over the Billing list
        (?modal=1). A link that navigates the page behind the overlay away
        is not what a click means there, so it is left out — detail-modal.js
        no longer has to reach in and delete it."""
        record = self._record()
        html = self._html(record, query='?modal=1')

        self.assertNotIn('View Order', html)
        self.assertIn('Verify Payment', html)  # the rest of the page is intact

    def test_the_detail_modal_script_no_longer_strips_a_view_order_link(self):
        script = (
            open('static/js/detail-modal.js', encoding='utf-8').read()
        )
        self.assertNotIn('a[data-detail-modal="Order"]', script)

    # -- Activity ----------------------------------------------------------

    def test_activity_lists_only_what_the_record_can_prove(self):
        record = self._record(
            status=BillingRecord.PaymentStatus.PAID,
            method=BillingRecord.PaymentMethod.GCASH,
            reference='GC-9', proof=True,
        )
        html = self._html(record)

        self.assertIn('Activity', html)
        self.assertIn('Bill created', html)
        self.assertIn('Payment verified', html)
        # M4 — no BillingStatusHistory table exists, and the page says so
        # rather than presenting the current fields as a full audit trail.
        self.assertIn('Billing keeps no status', html)

    def test_activity_and_order_heading_use_billing_scoped_classes(self):
        """D2 — .od-log / .od-when / .od-head are defined only inside
        sales/transaction_detail.html's own <style>, so on this page they
        rendered as a browser-numbered list and an unstyled heading row. The
        page uses its own .bill-* rules instead, and defines them."""
        record = self._record(status=BillingRecord.PaymentStatus.PAID)
        html = self._html(record)

        for cls in ('bill-log', 'bill-when', 'bill-head'):
            self.assertIn(f'class="{cls}"', html)
            self.assertIn(f'.{cls} ', html)  # the rule is on the page too
        for cls in ('od-log', 'od-when', 'od-head'):
            self.assertNotRegex(html, rf'class="[^"]*\b{cls}\b')

    def test_cancelled_without_reject_reason_invents_no_decision(self):
        """D5 — a Cancelled bill with a date_confirmed but no reject_reason
        is either a verified payment that was voided, or a rejection from
        before reject_reason existed that was voided. The row cannot say
        which, so Activity lists neither — only what is on file.
        (reject_reason is a non-null CharField, default '': "no reason" is
        the empty string on this schema, never NULL.)"""
        record = self._record(
            status=BillingRecord.PaymentStatus.CANCELLED, confirmed=True,
            reject_reason='',
            order_status=SalesTransaction.OrderStatus.CANCELLED,
        )
        record.voided_by = self.owner
        record.date_voided = timezone.now()
        record.save(update_fields=['voided_by', 'date_voided'])
        self.assertIsNotNone(record.date_confirmed)
        html = self._html(record)
        log = html.partition('class="bill-log"')[2].partition('</ol>')[0]

        self.assertIn('Bill created', log)
        self.assertIn('Bill voided', log)
        self.assertNotIn('Payment verified', log)
        self.assertNotIn('Payment rejected', log)

    def test_cancelled_with_reject_reason_keeps_the_rejection(self):
        """D5 — a reject_reason is only ever written by a rejection, so that
        event is still proven and still listed."""
        record = self._record(
            status=BillingRecord.PaymentStatus.CANCELLED, confirmed=True,
            reject_reason='Screenshot unreadable.',
            order_status=SalesTransaction.OrderStatus.CANCELLED,
        )
        html = self._html(record)
        log = html.partition('class="bill-log"')[2].partition('</ol>')[0]

        self.assertIn('Payment rejected', log)
        self.assertIn('Screenshot unreadable.', log)
        self.assertNotIn('Payment verified', log)

    def test_a_reopened_rejection_is_not_reconstructed(self):
        """M4, the limitation stated as a test: billing_confirm clears
        confirmed_by / date_confirmed / reject_reason when a rejected
        payment is re-opened, so nothing survives to date or attribute that
        rejection. The Activity block must not invent it.
        """
        record = self._record(
            status=BillingRecord.PaymentStatus.REJECTED,
            reject_reason='Screenshot unreadable.',
        )
        self.client.force_login(self.owner)
        self.client.post(
            reverse('billing_confirm', args=[record.pk]), {'status': 'Pending'}
        )
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

        html = self._html(record)
        self.assertIn('Bill created', html)
        self.assertNotIn('Payment rejected', html)
        self.assertNotIn('Screenshot unreadable.', html)

    def test_refund_activity_comes_from_the_append_only_refund_events(self):
        record = self._record(status=BillingRecord.PaymentStatus.PARTIALLY_REFUNDED)
        self._refund(record, '125.00')
        html = self._html(record)

        self.assertIn('Refund approved', html)
        self.assertIn('Requested at the counter.', html)

    def test_a_pending_refund_is_reported_not_decided(self):
        record = self._record(status=BillingRecord.PaymentStatus.PAID)
        self._refund(record, '125.00', status=Refund.Status.PENDING)
        html = self._html(record)

        self.assertIn('A refund request is awaiting approval.', html)
        self.assertIn('Approve or reject it on the order', html)
        self.assertNotIn('name="refund_method"', html)


class BillingDoesNotOwnTheOrderTests(TestCase):
    """The responsibility boundary, from the other side.

    Billing may write to BillingRecord. Everything it shows from
    SalesTransaction is read-only, and every order mutation is reached by
    navigating to the order — so the controls Billing gave up have to still
    be there when you get there. Phase 3 changed no Sales & Orders view,
    template branch or permission; these checks are what would catch it if
    a later edit did.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='bdo-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='BDO Branch', location='Baliuag', owner=cls.owner
        )
        cls.txn = SalesTransaction.objects.create(
            branch=cls.branch, total_amount=Decimal('300.00'),
            transaction_type=SalesTransaction.TransactionType.ONLINE,
        )

    def _order_html(self, query=''):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('transaction_detail', args=[self.txn.pk]) + query
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_order_detail_keeps_its_own_status_control(self):
        html = self._order_html()
        self.assertIn('od-status-select', html)
        self.assertIn('Change order status', html)
        self.assertIn('order-progress-track', html)

    def test_order_status_is_still_mutable_from_sales_and_orders(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('transaction_set_status', args=[self.txn.pk, 'Approved']),
            {'expected_status': 'Pending'},
        )
        self.assertEqual(response.status_code, 302)
        self.txn.refresh_from_db()
        self.assertEqual(
            self.txn.order_status, SalesTransaction.OrderStatus.APPROVED
        )

    def test_billing_exposes_no_order_status_endpoint(self):
        """Not "the button is hidden" — there is no order-status form on the
        Billing page at all, for any of the statuses."""
        record = self.txn.billing_record
        self.client.force_login(self.owner)
        html = self.client.get(
            reverse('billing_detail', args=[record.pk])
        ).content.decode()

        for status in SalesTransaction.OrderStatus.values:
            self.assertNotIn(
                reverse('transaction_set_status', args=[self.txn.pk, status]),
                html,
            )

    def test_refund_and_pwd_review_remain_on_the_order(self):
        record = self.txn.billing_record
        self.client.force_login(self.owner)
        billing_html = self.client.get(
            reverse('billing_detail', args=[record.pk])
        ).content.decode()

        for url in (
            reverse('transaction_refund', args=[self.txn.pk]),
            reverse('transaction_pwd_request_review', args=[self.txn.pk]),
        ):
            self.assertNotIn(url, billing_html)
