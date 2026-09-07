import threading
from importlib import import_module

from django.conf import settings
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.db import connections
from django.test import Client, RequestFactory, TestCase, TransactionTestCase

from accounts.models import User
from billing.models import BillingRecord
from billing.views import billing_confirm
from branches.models import Branch
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
