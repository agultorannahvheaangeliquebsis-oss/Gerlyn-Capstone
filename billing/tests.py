import threading
from importlib import import_module

from django.conf import settings
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.db import connections
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import BranchManagerProfile, StaffProfile, User
from billing.models import BillingRecord
from billing.serializers import BillingConfirmSerializer
from billing.views import billing_confirm
from branches.models import Branch
from core.module_permissions import BILLING, DEFAULT_STAFF_MODULES
from sales.models import SalesTransaction

SessionStore = import_module(settings.SESSION_ENGINE).SessionStore


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
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Paid'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Paid')
        self.assertEqual(rec.confirmed_by, self.owner)

    def test_pending_to_rejected_is_allowed(self):
        rec = self._record()
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Rejected'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Rejected')

    def test_rejected_can_be_reopened_to_pending(self):
        rec = self._record()
        self._set_status(rec, 'Rejected', confirmed_by=self.owner)
        self.login('gerlyn').post(f'/billing/{rec.pk}/confirm/', {'status': 'Pending'})
        rec.refresh_from_db()
        self.assertEqual(rec.status, 'Pending')
        self.assertIsNone(rec.confirmed_by, 're-open clears the stale confirmation')
        self.assertIsNone(rec.date_confirmed)

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

    def _serializer(self, record, user, new_status='Paid'):
        request = RequestFactory().patch(
            f'/api/billing/{record.pk}/confirm/', {'status': new_status}
        )
        request.user = user
        return BillingConfirmSerializer(
            record, data={'status': new_status}, context={'request': request}
        )

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
