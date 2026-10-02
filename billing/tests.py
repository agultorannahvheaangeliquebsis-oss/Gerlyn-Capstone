import base64
import shutil
import tempfile
import threading
from decimal import Decimal
from unittest import mock
from importlib import import_module

from django.conf import settings
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connections
from django.test import (
    Client, RequestFactory, TestCase, TransactionTestCase, override_settings,
)
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from django.views.static import serve

from accounts.models import BranchManagerProfile, StaffProfile, User
from billing.models import BillingRecord
from billing.serializers import BillingConfirmSerializer, BillingRecordSerializer
from billing.views import billing_confirm
from branches.models import Branch
from core.module_permissions import BILLING, DEFAULT_STAFF_MODULES
from sales.models import SalesTransaction

SessionStore = import_module(settings.SESSION_ENGINE).SessionStore


def _valid_png_file(name='receipt.png'):
    """A genuinely valid PNG — the upload check opens it with Pillow, which
    correctly rejects the hand-crafted _PNG_BYTES used for byte comparisons."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
    return ContentFile(buf.getvalue(), name=name)


class BillingListOutstandingTotalTests(TestCase):
    """F-12 — billing_list computed the outstanding total from the queryset
    *after* the tab's status filter had been applied, so on the Paid or
    Rejected tab it summed an empty set and rendered P0.00. The tab counts
    right above it are taken from the pre-filter queryset and stayed correct;
    the outstanding figure now follows the same pattern, so it reads the same
    (correct) amount on every tab.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='ot-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='OT Branch', location='Baliuag', owner=cls.owner
        )

        # Two Pending (100 + 50), one Paid (200), one Rejected (400).
        # Only the Pending records are "outstanding": 150.00, no matter
        # which tab is selected.
        cls.EXPECTED_OUTSTANDING = Decimal('150.00')
        cls._make_record(100, BillingRecord.PaymentStatus.PENDING)
        cls._make_record(50, BillingRecord.PaymentStatus.PENDING)
        cls._make_record(200, BillingRecord.PaymentStatus.PAID)
        cls._make_record(400, BillingRecord.PaymentStatus.REJECTED)

    @classmethod
    def _make_record(cls, amount, status):
        # billing.signals auto-creates the record in Pending with
        # amount_due = total_amount; flip the status afterwards if needed.
        txn = SalesTransaction.objects.create(
            branch=cls.branch, staff=None, total_amount=amount,
        )
        record = txn.billing_record
        if status != BillingRecord.PaymentStatus.PENDING:
            record.status = status
            record.save(update_fields=['status'])
        return record

    def _outstanding_on_tab(self, status):
        client = Client()
        client.force_login(self.owner)
        params = {'status': status} if status else {}
        response = client.get('/billing/', params)
        self.assertEqual(response.status_code, 200)
        return response.context['outstanding']

    def test_outstanding_total_is_correct_and_tab_independent(self):
        for tab in ('', 'Pending', 'Paid', 'Rejected'):
            with self.subTest(tab=tab or 'All'):
                self.assertEqual(
                    self._outstanding_on_tab(tab),
                    self.EXPECTED_OUTSTANDING,
                    f'outstanding total wrong on the {tab or "All"} tab',
                )


class BillingConfirmStateMachineTests(TestCase):
    """D-1 — billing_confirm must enforce BillingRecord.ALLOWED_STATUS_TRANSITIONS.

    Before this guard, billing_confirm accepted any target that was one of the
    four valid status strings, so a record could be flipped Paid -> Rejected
    -> Paid indefinitely, each pass overwriting confirmed_by / date_confirmed.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)
        cls.owner = User.objects.get(username='gerlyn')  # can access every branch

    def login(self, username):
        client = Client()
        client.force_login(User.objects.get(username=username))
        return client

    def _record(self):
        return (
            BillingRecord.objects
            .select_related('transaction')
            .filter(status=BillingRecord.PaymentStatus.PENDING)
            .first()
        )

    def _set_status(self, record, status, confirmed_by=None):
        record.status = status
        record.confirmed_by = confirmed_by
        record.save(update_fields=['status', 'confirmed_by'])

    # -- allowed ----------------------------------------------------------------

    def test_pending_to_paid_is_allowed(self):
        rec = self._record()
        # The proof-of-payment gate (each method's required reference number
        # and/or proof, BillingRecord.missing_payment_evidence) is
        # orthogonal to what this test checks — that the state machine
        # allows Pending -> Paid at all — so satisfy it explicitly rather
        # than depend on which Pending record seed_demo hands back first.
        if rec.payment_method in BillingRecord.REFERENCE_REQUIRED_METHODS and not rec.reference_number:
            rec.reference_number = 'TEST-REF-001'
            rec.save(update_fields=['reference_number'])
        if rec.payment_method in BillingRecord.PROOF_REQUIRED_METHODS and not rec.proof_of_payment:
            rec.proof_of_payment.save('proof.png', _valid_png_file(), save=True)
        with mock.patch('billing.notifications.notify_payment_decision'):
            self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Paid'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Paid')
        self.assertEqual(rec.confirmed_by, self.owner)

    def test_pending_to_rejected_is_allowed(self):
        rec = self._record()
        self.login('gerlyn').post(
            f'/billing/{rec.pk}/confirm/', {'status': 'Rejected', 'reason': 'Reference number does not match.'}
        )
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Rejected')
        self.assertEqual(rec.reject_reason, 'Reference number does not match.')

    def test_rejecting_without_a_reason_is_refused(self):
        rec = self._record()
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Rejected'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Pending', 'a reason is required to reject a payment')
        self.assertEqual(rec.reject_reason, '')

    def test_rejected_can_be_reopened_to_pending(self):
        rec = self._record()
        self._set_status(rec, 'Rejected', confirmed_by=self.owner)
        rec.reject_reason = 'Screenshot unreadable.'
        rec.save(update_fields=['reject_reason'])
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Pending'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Pending')
        self.assertIsNone(rec.confirmed_by, 're-open clears the stale confirmation')
        self.assertIsNone(rec.date_confirmed)
        self.assertEqual(rec.reject_reason, '', 're-open clears the stale rejection reason')

    # -- blocked --------------------------------------------------------------

    def test_paid_is_terminal_via_confirm(self):
        rec = self._record()
        self._set_status(rec, 'Paid', confirmed_by=self.owner)
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Rejected'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Paid', 'a Paid record cannot be flipped by billing_confirm')

    def test_rejected_cannot_jump_straight_to_paid(self):
        rec = self._record()
        self._set_status(rec, 'Rejected')
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Paid'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Rejected')

    def test_cancelled_is_terminal_via_confirm(self):
        rec = self._record()
        self._set_status(rec, 'Cancelled')
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Paid'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Cancelled')

    def test_tampered_status_value_is_rejected(self):
        rec = self._record()
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Whatever'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Pending')

    # -- model helper -------------------------------------------------------------

    def test_can_transition_to_matches_map(self):
        rec = BillingRecord(status=BillingRecord.PaymentStatus.PENDING)
        self.assertTrue(rec.can_transition_to('Paid'))
        self.assertTrue(rec.can_transition_to('Rejected'))
        self.assertFalse(rec.can_transition_to('Pending'))
        rec.status = BillingRecord.PaymentStatus.PAID
        self.assertFalse(rec.can_transition_to('Rejected'))


class BillingConfirmConcurrencyTests(TransactionTestCase):
    """D-1 — prove the select_for_update() lock in billing_confirm actually
    serialises two racing confirmations, not merely that the transition map
    is correct.

    TransactionTestCase, not TestCase: under TestCase the whole test runs in
    one un-committed transaction, so a second thread on its own connection
    can't see the row at all and the lock is unobservable. Here each thread's
    write really commits.

    The view is invoked through RequestFactory, NOT the Django test Client:
    the test Client's handler is not built for concurrent use from threads
    (shared template-capture and signal state), and driving it from two
    threads races on that harness state rather than on the DB row lock —
    which manifested as a flaky, wrong "both succeeded" even though the raw
    transaction.atomic() + select_for_update() serialise deterministically.
    RequestFactory runs the real view body with none of that machinery.

    Backend note: SELECT ... FOR UPDATE only takes a blocking row lock on a
    real RDBMS. On sqlite it is a no-op, so the race cannot be expressed and
    the test skips itself rather than passing without exercising anything.
    The project runs on MySQL/InnoDB, where it executes for real.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.owner = User.objects.create_user(
            username='race-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Race Branch', location='Baliuag', owner=self.owner
        )

    def _fresh_pending_record(self):
        # billing.signals auto-creates the BillingRecord in Pending on the
        # transaction's first save.
        txn = SalesTransaction.objects.create(branch=self.branch, total_amount=250)
        record = txn.billing_record
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        return record

    def _confirm_paid(self, pk, results, index, barrier):
        outcome = {'status_code': None, 'messages': [], 'error': None}
        try:
            request = self.factory.post(f'/billing/{pk}/confirm/', {'status': 'Paid'})
            request.user = self.owner
            request.session = SessionStore()
            request._messages = FallbackStorage(request)
            barrier.wait(timeout=10)          # both threads enter the view together
            response = billing_confirm(request, pk)
            outcome['status_code'] = response.status_code
            outcome['messages'] = [str(m.message) for m in request._messages]
        except Exception as exc:              # recorded, re-asserted on the main thread
            outcome['error'] = repr(exc)
        finally:
            connections.close_all()
        results[index] = outcome

    def test_two_racing_confirmations_serialise_to_one_paid(self):
        if connections['default'].vendor == 'sqlite':
            self.skipTest(
                'sqlite takes no row lock for SELECT ... FOR UPDATE, so this '
                'contention cannot be exercised. Runs for real on MySQL.'
            )

        # Repeat: a single race can serialise by luck even with a broken lock.
        for attempt in range(5):
            record = self._fresh_pending_record()
            results = [None, None]
            barrier = threading.Barrier(2)
            threads = [
                threading.Thread(
                    target=self._confirm_paid, args=(record.pk, results, i, barrier)
                )
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            ctx = f'(attempt {attempt}) {results}'
            self.assertNotIn(None, results, f'a confirmation thread never returned {ctx}')
            self.assertEqual([r['error'] for r in results], [None, None], ctx)
            self.assertEqual({r['status_code'] for r in results}, {302}, ctx)

            # One winner: the record is Paid, confirmed exactly once.
            record.refresh_from_db()
            self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID, ctx)
            self.assertEqual(record.confirmed_by_id, self.owner.id, ctx)
            self.assertIsNotNone(record.date_confirmed, ctx)

            # Exactly one thread confirmed; the other, once the lock released,
            # read Paid and was turned away by the state machine (Paid -> Paid
            # is not allowed). A no-op lock lets both read Pending and both
            # report success.
            successes = sum(
                1 for r in results
                if any('payment marked as Paid' in m for m in r['messages'])
            )
            rejections = sum(
                1 for r in results
                if any('cannot be changed to Paid' in m for m in r['messages'])
            )
            self.assertEqual(successes, 1, f'expected exactly one success {ctx}')
            self.assertEqual(rejections, 1, f'expected exactly one rejection {ctx}')


class BillingConfirmMakerCheckerTests(TestCase):
    """A-1 — maker/checker separation on non-cash payment.

    billing_confirm (and its DRF mirror BillingConfirmSerializer) must refuse
    a Paid/Rejected decision made by the same Staff member who recorded the
    sale — a second person has to check it. Owner and Branch Manager may
    confirm a sale they recorded themselves (the role, not the identity, is
    what the guard turns on). A sale with no recorded staff actor — an
    Owner/Manager-rung walk-in, or any online order, where
    SalesTransaction.staff is None — is confirmable by anyone; the guard's
    id comparison can never match a Staff user, so it simply doesn't fire
    (Finding A-3).
    """

    SELF_CONFIRM_ERROR = (
        "You can't confirm a payment you recorded yourself — ask another "
        "staff member, a manager, or the owner."
    )

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='mc-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='MC Branch', location='Baliuag', owner=cls.owner
        )
        cls.manager = User.objects.create_user(
            username='mc-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        BranchManagerProfile.objects.update_or_create(
            user=cls.manager, defaults={'manager_name': 'Mira', 'branch': cls.branch}
        )
        cls.staff = User.objects.create_user(
            username='mc-staff', password='pw', role=User.Role.STAFF
        )
        cls.other_staff = User.objects.create_user(
            username='mc-staff-2', password='pw', role=User.Role.STAFF
        )
        for staff, name in ((cls.staff, 'Sam'), (cls.other_staff, 'Tara')):
            StaffProfile.objects.update_or_create(
                user=staff, defaults={'staff_name': name, 'branch': cls.branch}
            )
            # Both are staff the Owner has granted the Billing module to —
            # otherwise ModulePermissionMiddleware turns them away before
            # billing_confirm runs and the maker/checker guard is never
            # reached. (New Staff default to DEFAULT_STAFF_MODULES, no Billing.)
            User.objects.filter(pk=staff.pk).update(
                allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
            )
            staff.refresh_from_db()
        cls.customer = User.objects.create_user(
            username='mc-customer', password='pw', role=User.Role.CUSTOMER
        )

    def _pending_record(self, staff):
        """A non-cash sale recorded by `staff` (pass None for an
        Owner/Manager-rung or online sale), plus the Pending BillingRecord
        billing.signals auto-creates for it."""
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=staff, total_amount=500,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        record = txn.billing_record
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        return record

    def _confirm(self, user, pk, new_status='Paid'):
        client = Client()
        client.force_login(user)
        return client.post(f'/billing/{pk}/confirm/', {'status': new_status})

    # -- blocked: the recorder cannot confirm their own -----------------------

    def test_staff_cannot_confirm_a_sale_they_recorded(self):
        record = self._pending_record(self.staff)
        response = self._confirm(self.staff, record.pk)

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        self.assertIsNone(record.confirmed_by)
        self.assertIsNone(record.date_confirmed)
        self.assertIn(
            self.SELF_CONFIRM_ERROR,
            [str(m) for m in get_messages(response.wsgi_request)],
        )

    def test_staff_cannot_reject_a_sale_they_recorded_either(self):
        record = self._pending_record(self.staff)
        self._confirm(self.staff, record.pk, new_status='Rejected')

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        self.assertIsNone(record.confirmed_by)

    # -- allowed: a second person, or a trusted role ------------------------

    def test_a_different_staff_member_can_confirm_it(self):
        record = self._pending_record(self.staff)
        self._confirm(self.other_staff, record.pk)

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.other_staff)

    def test_owner_can_confirm_their_own_non_cash_walk_in(self):
        # An Owner-rung sale carries staff=None (staff is only ever set for a
        # STAFF user), and the guard's role test is false for an Owner anyway
        # — so the override needs no branch of its own.
        record = self._pending_record(None)
        self._confirm(self.owner, record.pk)

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.owner)

    def test_branch_manager_can_confirm_their_own_non_cash_walk_in(self):
        record = self._pending_record(None)
        self._confirm(self.manager, record.pk)

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.manager)

    def test_a_staff_member_can_confirm_an_online_order(self):
        # Every online order has staff=None, so a Staff user's id can never
        # equal record.transaction.staff_id — the null case resolves to
        # "allowed" on its own. Proven here, not assumed.
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, customer=self.customer,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            mode_of_payment=SalesTransaction.ModeOfPayment.COD,
            total_amount=500,
        )
        record = txn.billing_record
        # Cash on Delivery is settled in person, so it needs no reference or
        # proof on file to be verified. The customer notification is stubbed:
        # its "₱" text can't be stored by this database's charset, an issue
        # unrelated to what this test is about.
        with mock.patch('billing.notifications.notify_payment_decision'):
            self._confirm(self.staff, record.pk)

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.staff)

    def test_original_recorder_can_still_reopen_a_rejected_record(self):
        # Rejected -> Pending records no confirmer (it clears it), so the
        # maker/checker guard must not apply to the re-open path.
        record = self._pending_record(self.staff)
        record.status = BillingRecord.PaymentStatus.REJECTED
        record.confirmed_by = self.other_staff
        record.date_confirmed = timezone.now()
        record.save(update_fields=['status', 'confirmed_by', 'date_confirmed'])

        self._confirm(self.staff, record.pk, new_status='Pending')

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        self.assertIsNone(record.confirmed_by)
        self.assertIsNone(record.date_confirmed)

    # -- DRF parity: same guard via BillingConfirmSerializer ---------------

    def _serializer(self, record, user, new_status='Paid', reason=''):
        data = {'status': new_status}
        if reason:
            data['reason'] = reason
        request = RequestFactory().patch(f'/api/billing/{record.pk}/confirm/', data)
        request.user = user
        return BillingConfirmSerializer(record, data=data, context={'request': request})

    def test_serializer_blocks_a_self_confirmation(self):
        record = self._pending_record(self.staff)
        serializer = self._serializer(record, self.staff)

        self.assertFalse(serializer.is_valid())
        self.assertIn(
            self.SELF_CONFIRM_ERROR,
            [str(e) for e in serializer.errors['status']],
        )
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

    def test_serializer_allows_a_different_staff_member(self):
        record = self._pending_record(self.staff)
        serializer = self._serializer(record, self.other_staff)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.other_staff)

    def test_serializer_requires_a_reason_to_reject(self):
        record = self._pending_record(self.staff)
        serializer = self._serializer(record, self.other_staff, new_status='Rejected')

        self.assertFalse(serializer.is_valid())
        self.assertIn('reason', serializer.errors)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

    def test_serializer_allows_reject_with_a_reason(self):
        record = self._pending_record(self.staff)
        serializer = self._serializer(
            record, self.other_staff, new_status='Rejected', reason='Reference number is wrong.'
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.REJECTED)
        self.assertEqual(record.reject_reason, 'Reference number is wrong.')

    def test_a_self_confirming_reason_still_does_not_bypass_the_maker_checker_gate(self):
        record = self._pending_record(self.staff)
        serializer = self._serializer(
            record, self.staff, new_status='Rejected', reason='Reference number is wrong.'
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn(
            self.SELF_CONFIRM_ERROR,
            [str(e) for e in serializer.errors['status']],
        )
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

    def test_serializer_rejects_cancelled_as_a_direct_target(self):
        """CANCELLED/PARTIALLY_REFUNDED/REFUNDED stay in
        ALLOWED_STATUS_TRANSITIONS only so sales.views (transaction_set_status
        / transaction_refund) can route through can_transition_to like every
        other caller — a human was never meant to reach them here. Before this
        guard, ChoiceField(choices=PaymentStatus.choices) accepted any of the
        six values, so an ordinary Billing-access API caller could PATCH a
        Pending record straight to Cancelled — or a Paid one to Refunded —
        bypassing the order's own cancellation/refund workflow (stock
        reversal, RefundEvent, the Owner/Branch-Manager-only refund approval)
        entirely, while Billing and Sales silently disagreed about the order's
        real state from then on.
        """
        record = self._pending_record(self.staff)
        serializer = self._serializer(record, self.other_staff, new_status='Cancelled')

        self.assertFalse(serializer.is_valid())
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

    def test_serializer_rejects_refunded_as_a_direct_target(self):
        record = self._pending_record(self.staff)
        record.status = BillingRecord.PaymentStatus.PAID
        record.confirmed_by = self.owner
        record.date_confirmed = timezone.now()
        record.save(update_fields=['status', 'confirmed_by', 'date_confirmed'])

        serializer = self._serializer(record, self.other_staff, new_status='Refunded')

        self.assertFalse(serializer.is_valid())
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)


class BillingConfirmReturnToOrderTests(TestCase):
    """Financials UX Audit Phase 1 — Order Detail (sales.views.
    transaction_detail) now posts to this same billing_confirm view with
    return_to=order, so Verify/Reject/Re-open can be done without leaving
    the order. Every redirect billing_confirm can take — success AND every
    early refusal — must honour that flag; a user who came from the order
    should never be bounced into Billing. No new transition/permission
    logic here: this only proves the existing redirects respect the flag.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='r2o-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='R2O Branch', location='Baliuag', owner=cls.owner
        )
        cls.staff = User.objects.create_user(
            username='r2o-staff', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff, defaults={'staff_name': 'Sam', 'branch': cls.branch}
        )
        cls.other_staff = User.objects.create_user(
            username='r2o-staff-2', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.other_staff, defaults={'staff_name': 'Tara', 'branch': cls.branch}
        )
        for staff in (cls.staff, cls.other_staff):
            # Same reason as BillingConfirmMakerCheckerTests — without the
            # Billing module, ModulePermissionMiddleware bounces a Staff
            # account before billing_confirm ever runs.
            User.objects.filter(pk=staff.pk).update(
                allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
            )
            staff.refresh_from_db()

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _pending_cash_record(self, staff=None):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=staff, total_amount=250,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        record = txn.billing_record
        record.payment_method = BillingRecord.PaymentMethod.CASH
        record.save(update_fields=['payment_method'])
        return record

    def test_verifying_from_order_returns_to_order_detail(self):
        record = self._pending_cash_record()
        with mock.patch('billing.notifications.notify_payment_decision'):
            response = self._client(self.owner).post(
                f'/billing/{record.pk}/confirm/',
                {'status': 'Paid', 'return_to': 'order'},
            )

        self.assertRedirects(
            response, reverse('transaction_detail', args=[record.transaction_id])
        )
        record.refresh_from_db()
        self.assertEqual(record.status, 'Paid')

    def test_rejecting_from_order_returns_to_order_detail(self):
        record = self._pending_cash_record()
        with mock.patch('billing.notifications.notify_payment_decision'):
            response = self._client(self.owner).post(
                f'/billing/{record.pk}/confirm/',
                {'status': 'Rejected', 'reason': 'Reference unreadable.', 'return_to': 'order'},
            )

        self.assertRedirects(
            response, reverse('transaction_detail', args=[record.transaction_id])
        )
        record.refresh_from_db()
        self.assertEqual(record.status, 'Rejected')

    def test_a_refused_self_confirmation_from_order_still_returns_to_the_order(self):
        # The maker/checker refusal is an early-exit branch distinct from the
        # success path below it — return_to has to be honoured there too, or
        # the one case staff hit most (trying to verify their own sale) is
        # the one that strands them in Billing instead of back on the order.
        record = self._pending_cash_record(staff=self.staff)
        response = self._client(self.staff).post(
            f'/billing/{record.pk}/confirm/',
            {'status': 'Paid', 'return_to': 'order'},
        )

        self.assertRedirects(
            response, reverse('transaction_detail', args=[record.transaction_id])
        )
        record.refresh_from_db()
        self.assertEqual(record.status, 'Pending')

    def test_a_different_staff_member_verifying_from_order_returns_to_the_order(self):
        record = self._pending_cash_record(staff=self.staff)
        with mock.patch('billing.notifications.notify_payment_decision'):
            response = self._client(self.other_staff).post(
                f'/billing/{record.pk}/confirm/',
                {'status': 'Paid', 'return_to': 'order'},
            )

        self.assertRedirects(
            response, reverse('transaction_detail', args=[record.transaction_id])
        )
        record.refresh_from_db()
        self.assertEqual(record.status, 'Paid')
        self.assertEqual(record.confirmed_by, self.other_staff)

    def test_without_return_to_the_existing_billing_detail_redirect_is_unchanged(self):
        record = self._pending_cash_record()
        with mock.patch('billing.notifications.notify_payment_decision'):
            response = self._client(self.owner).post(
                f'/billing/{record.pk}/confirm/', {'status': 'Paid'},
            )

        self.assertRedirects(response, reverse('billing_detail', args=[record.pk]))


class BillingConfirmAPIBranchScopeTests(TestCase):
    """F-02 — BillingConfirmAPIView.patch checked role but never branch
    scope: a Staff/Manager account could PATCH /api/billing/<pk>/confirm/
    for a record belonging to another branch just by knowing its pk, while
    the web view (billing_confirm) already refused the same request via
    user_can_access_branch. The fix routes the API through the same
    scoping helper this file's other API views already use
    (user_can_access_billing_record) and returns 404, matching how
    BillingRecordDetailAPIView and BillingPaymentAPIView already respond to
    an out-of-scope pk.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='cs-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch1 = Branch.objects.create(
            branch_name='CS Branch 1', location='Baliuag', owner=cls.owner
        )
        cls.branch2 = Branch.objects.create(
            branch_name='CS Branch 2', location='Baliuag', owner=cls.owner
        )
        cls.staff1 = User.objects.create_user(
            username='cs-staff1', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff1, defaults={'staff_name': 'Sid', 'branch': cls.branch1}
        )
        # cs-staff1 is a Staff the Owner has granted the Billing module to.
        # Since F-07, ModulePermissionMiddleware also gates the API confirm
        # endpoint, so without this a new Staff (DEFAULT_STAFF_MODULES, no
        # Billing) is redirected before branch scope is ever reached — and
        # this suite is about branch scope, not the module toggle.
        User.objects.filter(pk=cls.staff1.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
        )
        cls.staff1.refresh_from_db()
        cls.manager1 = User.objects.create_user(
            username='cs-manager1', password='pw', role=User.Role.BRANCH_MANAGER
        )
        BranchManagerProfile.objects.update_or_create(
            user=cls.manager1, defaults={'manager_name': 'Mona', 'branch': cls.branch1}
        )

    def _record_for(self, branch):
        """A Pending BillingRecord on `branch`. staff=None (an Owner/Manager
        -rung or online sale) so the maker/checker self-confirmation guard
        never fires here — this test is about branch scope, not that."""
        txn = SalesTransaction.objects.create(
            branch=branch, staff=None, total_amount=500,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        record = txn.billing_record
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        return record

    def _confirm_api(self, user, pk, new_status='Paid'):
        client = Client()
        client.force_login(user)
        return client.patch(
            f'/api/billing/{pk}/confirm/', {'status': new_status},
            content_type='application/json',
        )

    def test_staff_cannot_confirm_a_record_from_another_branch(self):
        record = self._record_for(self.branch2)

        response = self._confirm_api(self.staff1, record.pk)

        self.assertEqual(response.status_code, 404)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        self.assertIsNone(record.confirmed_by)

    def test_staff_can_still_confirm_a_record_from_their_own_branch(self):
        """The fix must not over-block a legitimate same-branch confirm."""
        record = self._record_for(self.branch1)

        response = self._confirm_api(self.staff1, record.pk)

        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.staff1)

    def test_manager_cannot_confirm_a_record_from_another_branch(self):
        record = self._record_for(self.branch2)

        response = self._confirm_api(self.manager1, record.pk)

        self.assertEqual(response.status_code, 404)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

    def test_manager_can_still_confirm_a_record_from_their_own_branch(self):
        record = self._record_for(self.branch1)

        response = self._confirm_api(self.manager1, record.pk)

        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)

    def test_owner_can_confirm_a_record_on_any_branch(self):
        """Owner is unrestricted in user_can_access_billing_record — the fix
        must not accidentally scope the Owner to a branch too."""
        for branch in (self.branch1, self.branch2):
            with self.subTest(branch=branch.branch_name):
                record = self._record_for(branch)

                response = self._confirm_api(self.owner, record.pk)

                self.assertEqual(response.status_code, 200)
                record.refresh_from_db()
                self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)

    def test_reproduces_the_reported_bug_is_now_fixed(self):
        """The exact repro from the bug report: a Staff account pinned to
        Branch 1 PATCHing a Branch 2 record used to succeed (200,
        confirmed_by set) where the equivalent web endpoint on the same
        record correctly refused it (302, still Pending). Both paths must
        now agree."""
        record = self._record_for(self.branch2)

        api_response = self._confirm_api(self.staff1, record.pk)
        self.assertEqual(api_response.status_code, 404)

        web_client = Client()
        web_client.force_login(self.staff1)
        web_response = web_client.post(
            f'/billing/{record.pk}/confirm/', {'status': 'Paid'}
        )
        self.assertEqual(web_response.status_code, 302)

        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        self.assertIsNone(record.confirmed_by)


class BillingAPIModulePermissionTests(TestCase):
    """F-07 — MODULE_REGISTRY only listed billing_* (the template url names),
    so ModulePermissionMiddleware turned a Staff account whose Owner revoked
    the Billing module away from /billing/ but let the same account reach
    /api/billing/<pk>/confirm/ (and the rest of the billing API) directly.
    The API url names are now in the Billing module's registry entry, so the
    one middleware gate covers both surfaces the same way — a redirect, with
    the record left untouched.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='f7b-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='F7B Branch', location='Baliuag', owner=cls.owner
        )
        # Two Staff on the same branch: one WITH the Billing module, one
        # WITHOUT. Both pass branch scope (F-02) — the only difference is the
        # module toggle this test is about.
        cls.staff_with = User.objects.create_user(
            username='f7b-staff-with', password='pw', role=User.Role.STAFF
        )
        cls.staff_without = User.objects.create_user(
            username='f7b-staff-without', password='pw', role=User.Role.STAFF
        )
        for user, name in ((cls.staff_with, 'Wanda'), (cls.staff_without, 'Otto')):
            StaffProfile.objects.update_or_create(
                user=user, defaults={'staff_name': name, 'branch': cls.branch}
            )
        User.objects.filter(pk=cls.staff_with.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
        )
        User.objects.filter(pk=cls.staff_without.pk).update(
            allowed_modules=list(DEFAULT_STAFF_MODULES)  # no BILLING
        )

    def _pending_record(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, staff=None, total_amount=500,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        record = txn.billing_record
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        return record

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_staff_without_billing_module_is_refused_the_api_confirm(self):
        record = self._pending_record()

        response = self._client(self.staff_without).patch(
            f'/api/billing/{record.pk}/confirm/', {'status': 'Paid'},
            content_type='application/json',
        )

        # APIs return an explicit authorization error; nothing is written.
        self.assertEqual(response.status_code, 403)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)
        self.assertIsNone(record.confirmed_by)

    def test_api_and_web_now_agree_for_a_module_revoked_staff(self):
        record = self._pending_record()

        api_resp = self._client(self.staff_without).patch(
            f'/api/billing/{record.pk}/confirm/', {'status': 'Paid'},
            content_type='application/json',
        )
        web_resp = self._client(self.staff_without).post(
            f'/billing/{record.pk}/confirm/', {'status': 'Paid'}
        )

        self.assertEqual(api_resp.status_code, 403)
        self.assertEqual(web_resp.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

    def test_staff_without_billing_module_is_refused_the_api_list(self):
        response = self._client(self.staff_without).get('/api/billing/')
        self.assertEqual(response.status_code, 403)

    def test_staff_with_billing_module_still_reaches_the_api(self):
        """The gate must not over-block: a Staff the Owner granted Billing to
        confirms through the API exactly as before."""
        record = self._pending_record()

        response = self._client(self.staff_with).patch(
            f'/api/billing/{record.pk}/confirm/', {'status': 'Paid'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PAID)
        self.assertEqual(record.confirmed_by, self.staff_with)

    def test_owner_is_never_module_gated_on_the_api(self):
        record = self._pending_record()

        response = self._client(self.owner).patch(
            f'/api/billing/{record.pk}/confirm/', {'status': 'Paid'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)


# A real 1x1 PNG — ImageField doesn't validate bytes on a model .save(), but a
# genuine image keeps the fixture honest and lets us byte-compare the stream.
_PNG_BYTES = base64.b64decode(
    b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII='
)
_PROOF_MEDIA_ROOT = tempfile.mkdtemp(prefix='f06-payment-proofs-')


@override_settings(MEDIA_ROOT=_PROOF_MEDIA_ROOT)
class PaymentProofAuthGateTests(TestCase):
    """F-06 — GCash payment-proof screenshots were handed to django.views.
    static.serve by the r'^media/...' route in gerlyn_is/urls.py with no
    authentication and no middleware in front of it: anyone who had or guessed
    a path under /media/payment_proofs/ — signed in or not — could read a
    customer's name, reference number and payment amount, i.e. the billing
    audit trail's evidence.

    The proofs are now reachable ONLY through billing.views.billing_proof,
    which applies the same scoping (user_can_access_billing_record) the billing
    API views already use, and the /media/ route no longer matches
    payment_proofs/ at all. Every other prefix under MEDIA_ROOT (product
    images, avatars) is still served exactly as before.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _PROOF_MEDIA_ROOT, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pp-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch1 = Branch.objects.create(
            branch_name='PP Branch 1', location='Baliuag', owner=cls.owner
        )
        cls.branch2 = Branch.objects.create(
            branch_name='PP Branch 2', location='Baliuag', owner=cls.owner
        )
        cls.staff1 = cls._make_staff('pp-staff1', 'Sena', cls.branch1)
        cls.staff2 = cls._make_staff('pp-staff2', 'Toby', cls.branch2)

        # A GCash sale on branch 1 with an uploaded proof. staff=None so the
        # maker/checker guard is irrelevant here — this is about who may read
        # the file, not who may confirm the payment.
        txn = SalesTransaction.objects.create(
            branch=cls.branch1, staff=None, total_amount=500,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        cls.record = txn.billing_record
        cls.record.proof_of_payment.save(
            'proof.png', ContentFile(_PNG_BYTES), save=True
        )
        # e.g. "payment_proofs/proof.png" — the exact path the old raw route
        # used to serve.
        cls.proof_name = cls.record.proof_of_payment.name

    @classmethod
    def _make_staff(cls, username, name, branch):
        user = User.objects.create_user(
            username=username, password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=user, defaults={'staff_name': name, 'branch': branch}
        )
        # Billing is one of the modules a new Staff account does NOT get by
        # default; without it ModulePermissionMiddleware redirects before
        # billing_proof runs (billing_proof is registered under the Billing
        # module, same as billing_detail).
        User.objects.filter(pk=user.pk).update(
            allowed_modules=[*DEFAULT_STAFF_MODULES, BILLING]
        )
        user.refresh_from_db()
        return user

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _proof_url(self, pk=None):
        return f'/billing/{pk or self.record.pk}/proof/'

    # -- the new view: allowed --------------------------------------------------

    def test_same_branch_staff_gets_the_file_streamed(self):
        response = self._client(self.staff1).get(self._proof_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), _PNG_BYTES)

    def test_owner_gets_the_file_for_any_branch(self):
        response = self._client(self.owner).get(self._proof_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), _PNG_BYTES)

    def test_the_response_is_never_cached(self):
        """The auth check above only covers this one request — without
        no-store/no-cache, the browser (or a shared proxy) could keep its
        own copy of the image on disk, readable later with no auth check
        at all. Enforced project-wide by
        accounts.middleware.NoCacheForAuthenticatedMiddleware for every
        authenticated response, not anything billing-specific."""
        response = self._client(self.owner).get(self._proof_url())

        self.assertEqual(
            response['Cache-Control'], 'no-store, no-cache, must-revalidate, private'
        )

    # -- the new view: refused -----------------------------------------------

    def test_other_branch_staff_gets_404_not_the_file(self):
        response = self._client(self.staff2).get(self._proof_url())

        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_request_is_redirected_to_login(self):
        response = Client().get(self._proof_url())

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    # -- the other proof endpoint (payment_proof_download) ---------------------
    # Same underlying file, reached by a second, older URL
    # (/billing/<pk>/payment-proof/) that a few templates still link to
    # directly (e.g. "download" links) instead of going through billing_proof.
    # Same auth gate, same caching risk.

    def _download_url(self, pk=None):
        return f'/billing/{pk or self.record.pk}/payment-proof/'

    def test_download_endpoint_is_also_never_cached(self):
        response = self._client(self.owner).get(self._download_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Cache-Control'], 'no-store, no-cache, must-revalidate, private'
        )

    def test_download_endpoint_refuses_another_branch(self):
        response = self._client(self.staff2).get(self._download_url())

        self.assertEqual(response.status_code, 404)

    def test_download_endpoint_requires_login(self):
        response = Client().get(self._download_url())

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_record_with_no_proof_uploaded_is_404(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch1, staff=None, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )
        response = self._client(self.staff1).get(
            self._proof_url(txn.billing_record.pk)
        )

        self.assertEqual(response.status_code, 404)

    # -- the raw /media/ path is closed ------------------------------------

    def test_direct_get_on_the_old_raw_media_path_no_longer_serves_it(self):
        raw_path = f'/media/{self.proof_name}'

        # The original repro: unauthenticated.
        self.assertEqual(Client().get(raw_path).status_code, 404)
        # And even for the Owner — the route simply doesn't match anymore.
        self.assertEqual(
            self._client(self.owner).get(raw_path).status_code, 404
        )

    def test_media_route_still_serves_every_other_prefix(self):
        # payment_proofs/ no longer resolves to a view at all...
        with self.assertRaises(Resolver404):
            resolve('/media/payment_proofs/anything.png')
        # ...while product images and avatars still route to the static server
        # (a real request would 404 only if the file is missing, not because
        # the route is gone).
        for path in (
            '/media/products/2026/01/widget.png',
            '/media/avatars/7/avatar.png',
        ):
            self.assertEqual(resolve(path).func, serve, path)

    # -- callers point at the new view, not the raw URL -------------------

    def test_billing_detail_template_links_the_gated_view(self):
        html = self._client(self.owner).get(
            f'/billing/{self.record.pk}/'
        ).content.decode()

        self.assertIn(self._proof_url(), html)
        self.assertNotIn('/media/payment_proofs/', html)

    def test_api_serializer_emits_the_gated_view_url(self):
        data = BillingRecordSerializer(self.record).data

        self.assertEqual(data['proofOfPayment'], self._proof_url())
        self.assertNotIn('payment_proofs', data['proofOfPayment'])


class BillingCreateValidationTests(TestCase):
    """F-08a — billing_create takes payment_method straight from POST and
    writes it onto BillingRecord, a NOT NULL column. A POST that omits the
    field used to reach the model save and 500 with an IntegrityError. It
    must be caught as a form error, with nothing persisted.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='bc-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='BC Branch', location='Baliuag', owner=cls.owner
        )

    def _pending_txn(self):
        """A sale plus the Pending BillingRecord billing.signals auto-creates
        for it (payment_method defaults to Cash there)."""
        return SalesTransaction.objects.create(
            branch=self.branch, total_amount=250,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )

    def _client(self):
        client = Client()
        client.force_login(self.owner)
        return client

    def test_missing_payment_method_reopens_the_form_without_persisting(self):
        txn = self._pending_txn()
        record = txn.billing_record
        original_method = record.payment_method

        response = self._client().post(
            f'/billing/create/{txn.pk}/',
            {'reference_number': '1000000000001'},  # no payment_method at all
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        self.assertIn(
            'Choose a valid payment method to record this payment.',
            [str(m) for m in get_messages(response.wsgi_request)],
        )
        record.refresh_from_db()
        self.assertEqual(record.payment_method, original_method)
        self.assertEqual(record.reference_number, '')
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)

    def test_a_blank_payment_method_is_rejected_the_same_way(self):
        txn = self._pending_txn()
        response = self._client().post(
            f'/billing/create/{txn.pk}/',
            {'payment_method': '', 'reference_number': '1000000000002'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        txn.billing_record.refresh_from_db()
        self.assertEqual(txn.billing_record.reference_number, '')

    def test_a_supplied_payment_method_still_saves_exactly_as_before(self):
        """Regression guard — the presence check must not disturb a normal
        submit."""
        txn = self._pending_txn()
        record = txn.billing_record

        response = self._client().post(
            f'/billing/create/{txn.pk}/',
            {'payment_method': 'GCash', 'reference_number': '1000000000009',
             'proof_of_payment': _valid_png_file()},
        )

        self.assertRedirects(response, f'/billing/{record.pk}/')
        record.refresh_from_db()
        self.assertEqual(record.payment_method, 'GCash')
        self.assertEqual(record.reference_number, '1000000000009')

    def test_an_unrecognized_payment_method_is_rejected(self):
        """A value outside BillingRecord.PaymentMethod (a tampered POST, or
        a typo like "Paid") used to save verbatim — a payment method the
        counter can't act on, same class of bug as an unpaid order reading
        "Paid"."""
        txn = self._pending_txn()
        record = txn.billing_record
        original_method = record.payment_method

        response = self._client().post(
            f'/billing/create/{txn.pk}/',
            {'payment_method': 'Paid', 'reference_number': '1000000000003'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        record.refresh_from_db()
        self.assertEqual(record.payment_method, original_method)
        self.assertEqual(record.reference_number, '')

    def test_a_non_image_proof_of_payment_is_rejected(self):
        """proof_of_payment used to be assigned straight from request.FILES
        with no validation — an HTML file (or anything else) saved and
        served back as-is."""
        txn = self._pending_txn()
        record = txn.billing_record

        response = self._client().post(
            f'/billing/create/{txn.pk}/',
            {
                'payment_method': 'GCash',
                'reference_number': '1000000000004',
                'proof_of_payment': ContentFile(
                    b'<html><body><script>alert(1)</script></body></html>',
                    name='receipt.html',
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        record.refresh_from_db()
        self.assertFalse(record.proof_of_payment)
        self.assertEqual(record.reference_number, '')

    def test_a_real_image_proof_of_payment_still_saves(self):
        # _PNG_BYTES is a hand-crafted, technically-malformed PNG (bad IDAT
        # checksum) — fine for the byte-identity comparisons above, but the
        # new Pillow-based check correctly rejects it, so a genuinely valid
        # image is built here instead to prove a real upload still works.
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
        valid_png_bytes = buf.getvalue()

        txn = self._pending_txn()
        record = txn.billing_record

        response = self._client().post(
            f'/billing/create/{txn.pk}/',
            {
                'payment_method': 'GCash',
                'reference_number': '1000000000005',
                'proof_of_payment': ContentFile(valid_png_bytes, name='receipt.png'),
            },
        )

        self.assertRedirects(response, f'/billing/{record.pk}/')
        record.refresh_from_db()
        self.assertTrue(record.proof_of_payment)
        self.assertEqual(record.status, BillingRecord.PaymentStatus.PENDING)


class BillingCreateCancelLinkTests(TestCase):
    """billing_create builds a two-front-doors breadcrumb (Sales & Orders vs
    Billing) from the `from` flag on the entry link, precisely so a visitor
    who opened this form from Billing is not quietly dropped back onto the
    order page instead (see the comment in billing.views.billing_create).
    The template's Cancel button is a second, separate exit from the same
    page and must honour that same flag rather than hardcoding the order.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='bccl-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='BCCL Branch', location='Baliuag', owner=cls.owner
        )

    def _client(self):
        client = Client()
        client.force_login(self.owner)
        return client

    def test_cancel_returns_to_order_detail_when_opened_from_the_order(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        response = self._client().get(f'/billing/create/{txn.pk}/')
        self.assertContains(response, f'href="/sales/{txn.pk}/"')

    def test_cancel_returns_to_billing_detail_when_opened_from_billing(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, total_amount=100,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        record = txn.billing_record
        response = self._client().get(f'/billing/create/{txn.pk}/?from=billing')
        self.assertContains(response, f'href="/billing/{record.pk}/"')


class PaymentEvidencePerMethodTests(TestCase):
    """What each method has to have on file before it can be saved or
    verified: GCash and Bank Transfer need a reference number AND a proof,
    Card needs a proof, and Cash / Cash on Delivery / Pay at Store need
    neither. One rule (BillingRecord.missing_payment_evidence) shared by the
    web form, the API and Mark as Paid."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pe-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='PE Branch', location='Baliuag', owner=cls.owner
        )

    def _txn(self, method=SalesTransaction.ModeOfPayment.GCASH):
        return SalesTransaction.objects.create(
            branch=self.branch, total_amount=250, mode_of_payment=method,
        )

    def _client(self):
        client = Client()
        client.force_login(self.owner)
        return client

    def _save_payment(self, txn, **data):
        return self._client().post(f'/billing/create/{txn.pk}/', data)

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    # -- the rule itself ----------------------------------------------------

    def test_the_rule_per_method(self):
        need = BillingRecord.missing_payment_evidence
        M = BillingRecord.PaymentMethod
        self.assertEqual(need(M.GCASH, '', False), ['reference number', 'proof of payment'])
        self.assertEqual(need(M.GCASH, '1000000000001', False), ['proof of payment'])
        self.assertEqual(need(M.GCASH, '', True), ['reference number'])
        self.assertEqual(need(M.GCASH, '1000000000001', True), [])
        self.assertEqual(need(M.BANK_TRANSFER, 'BT-1', False), ['proof of payment'])
        self.assertEqual(need(M.CARD, '', False), ['proof of payment'])
        self.assertEqual(need(M.CARD, '', True), [])
        for method in (M.CASH, M.COD, M.PAY_AT_STORE):
            self.assertEqual(need(method, '', False), [])

    def test_a_blank_or_whitespace_reference_counts_as_missing(self):
        need = BillingRecord.missing_payment_evidence
        self.assertEqual(
            need(BillingRecord.PaymentMethod.GCASH, '   ', True), ['reference number']
        )

    # -- Save Payment (billing_create) ----------------------------------------

    def test_gcash_without_a_reference_is_not_saved(self):
        txn = self._txn()
        response = self._save_payment(
            txn, payment_method='GCash', proof_of_payment=_valid_png_file()
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'billing/billing_form.html')
        self.assertTrue(any('reference number' in m for m in self._messages(response)))
        record = txn.billing_record
        record.refresh_from_db()
        self.assertFalse(record.proof_of_payment)

    def test_gcash_without_a_proof_is_not_saved(self):
        txn = self._txn()
        response = self._save_payment(
            txn, payment_method='GCash', reference_number='1000000000001'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('proof of payment' in m for m in self._messages(response)))
        record = txn.billing_record
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '')

    def test_bank_transfer_needs_both_too(self):
        txn = self._txn()
        response = self._save_payment(
            txn, payment_method='Bank Transfer', reference_number='BT-1'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any('proof of payment' in m for m in self._messages(response)))

    def test_gcash_with_both_saves(self):
        txn = self._txn()
        response = self._save_payment(
            txn, payment_method='GCash', reference_number='1000000000007',
            proof_of_payment=_valid_png_file(),
        )

        record = txn.billing_record
        self.assertRedirects(response, f'/billing/{record.pk}/')
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '1000000000007')
        self.assertTrue(record.proof_of_payment)

    def test_a_proof_already_on_file_is_not_demanded_again(self):
        txn = self._txn()
        record = txn.billing_record
        record.proof_of_payment.save('first.png', _valid_png_file(), save=True)

        response = self._save_payment(
            txn, payment_method='GCash', reference_number='1000000000008'
        )

        self.assertRedirects(response, f'/billing/{record.pk}/')
        record.refresh_from_db()
        self.assertEqual(record.reference_number, '1000000000008')

    def test_card_needs_a_proof_but_not_a_reference(self):
        txn = self._txn(SalesTransaction.ModeOfPayment.CARD)
        refused = self._save_payment(txn, payment_method='Card')
        self.assertEqual(refused.status_code, 200)
        self.assertTrue(any('proof of payment' in m for m in self._messages(refused)))

        saved = self._save_payment(
            txn, payment_method='Card', reference_number='typed-and-dropped',
            proof_of_payment=_valid_png_file(),
        )
        record = txn.billing_record
        self.assertRedirects(saved, f'/billing/{record.pk}/')
        record.refresh_from_db()
        self.assertEqual(record.payment_method, 'Card')
        # Card has no reference to keep, so anything typed is cleared.
        self.assertEqual(record.reference_number, '')
        self.assertTrue(record.proof_of_payment)

    def test_cash_saves_with_nothing(self):
        txn = self._txn(SalesTransaction.ModeOfPayment.CASH)
        response = self._save_payment(txn, payment_method='Cash')

        self.assertRedirects(response, f'/billing/{txn.billing_record.pk}/')

    def test_the_form_hands_the_page_the_same_lists_the_server_uses(self):
        txn = self._txn()
        response = self._client().get(f'/billing/create/{txn.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.context['reference_required_methods']),
            {'GCash', 'Bank Transfer'},
        )
        self.assertEqual(
            set(response.context['proof_required_methods']),
            {'GCash', 'Bank Transfer', 'Card'},
        )
        # Hidden until the method calls for them, and the copy no longer
        # calls the proof "optional".
        self.assertContains(response, 'id="reference_field" style="display:none;"')
        self.assertContains(response, 'id="proof_field" style="display:none;"')
        self.assertNotContains(response, 'optional screenshot')

    # -- Mark as Paid (billing_confirm) -------------------------------------

    def _record(self, method, reference='', proof=False):
        record = self._txn().billing_record
        record.payment_method = method
        record.reference_number = reference
        record.save()
        if proof:
            record.proof_of_payment.save('proof.png', _valid_png_file(), save=True)
        return record

    def _mark_paid(self, record):
        with mock.patch('billing.notifications.notify_payment_decision'):
            self._client().post(f'/billing/{record.pk}/confirm/', {'status': 'Paid'})
        record.refresh_from_db()
        return record.status

    def test_gcash_cannot_be_verified_on_a_reference_alone(self):
        record = self._record('GCash', reference='1000000000001')
        self.assertEqual(self._mark_paid(record), 'Pending')

    def test_gcash_cannot_be_verified_on_a_proof_alone(self):
        record = self._record('GCash', proof=True)
        self.assertEqual(self._mark_paid(record), 'Pending')

    def test_gcash_verifies_with_both(self):
        record = self._record('GCash', reference='1000000000001', proof=True)
        self.assertEqual(self._mark_paid(record), 'Paid')

    def test_card_verifies_on_a_proof_and_not_without(self):
        self.assertEqual(self._mark_paid(self._record('Card')), 'Pending')
        self.assertEqual(self._mark_paid(self._record('Card', proof=True)), 'Paid')

    def test_counter_settled_methods_verify_with_nothing(self):
        for method in ('Cash', 'Cash on Delivery', 'Pay at Store'):
            with self.subTest(method=method):
                self.assertEqual(self._mark_paid(self._record(method)), 'Paid')

    # -- the API serializers mirror it --------------------------------------

    def test_confirm_serializer_enforces_the_same_rule(self):
        request = RequestFactory().patch('/')
        request.user = self.owner
        short = BillingConfirmSerializer(
            self._record('GCash', reference='1000000000001'),
            data={'status': 'Paid'}, context={'request': request},
        )
        self.assertFalse(short.is_valid())

        complete = BillingConfirmSerializer(
            self._record('GCash', reference='1000000000002', proof=True),
            data={'status': 'Paid'}, context={'request': request},
        )
        self.assertTrue(complete.is_valid(), complete.errors)

    def test_payment_serializer_enforces_the_same_rule(self):
        from billing.serializers import BillingPaymentSerializer

        record = self._record('Cash')
        missing_proof = BillingPaymentSerializer(
            record, data={'paymentMethod': 'GCash', 'referenceNumber': '1000000000001'}
        )
        self.assertFalse(missing_proof.is_valid())

        card_no_proof = BillingPaymentSerializer(
            record, data={'paymentMethod': 'Card'}
        )
        self.assertFalse(card_no_proof.is_valid())

        cash = BillingPaymentSerializer(record, data={'paymentMethod': 'Cash'})
        self.assertTrue(cash.is_valid(), cash.errors)

    def test_payment_serializer_enforces_the_channel_restriction_too(self):
        """B-3 narrowed billing_create's own POST handling to the
        transaction's channel (a walk-in sale can't be "settled" as Cash on
        Delivery, nor an online order as a counter Cash/Card swipe), but
        BillingPaymentSerializer — the API's mirror of that same form — only
        ever checked paymentMethod against the full six-value enum. A client
        hitting PATCH /api/billing/<pk>/pay/ directly could record a payment
        method neither the counter nor the customer could have actually used,
        and it would then sync onto the sale's own mode_of_payment (X-2)."""
        from billing.serializers import BillingPaymentSerializer

        walkin_record = self._record('Cash')  # _txn() defaults to Physical.
        cod_on_walkin = BillingPaymentSerializer(
            walkin_record, data={'paymentMethod': 'Cash on Delivery'}
        )
        self.assertFalse(cod_on_walkin.is_valid())

        online_txn = SalesTransaction.objects.create(
            branch=self.branch, total_amount=250,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
        )
        online_record = online_txn.billing_record
        cash_on_online = BillingPaymentSerializer(
            online_record, data={'paymentMethod': 'Cash'}
        )
        self.assertFalse(cash_on_online.is_valid())

        # Regression guard: a channel-appropriate method still saves.
        cod_on_online = BillingPaymentSerializer(
            online_record, data={'paymentMethod': 'Cash on Delivery'}
        )
        self.assertTrue(cod_on_online.is_valid(), cod_on_online.errors)
