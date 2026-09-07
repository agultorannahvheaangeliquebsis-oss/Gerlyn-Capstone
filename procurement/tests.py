"""C-3 — procurement_update_status (the template view) must enforce
Procurement.ALLOWED_TRANSITIONS, the same map the DRF endpoint uses.

Before this guard the template view did `proc.status = request.POST['status'];
proc.save()` with no check: a Received PO could be forced back to Ordered, or
set to an arbitrary string. The DRF ProcurementStatusUpdateAPIView already
validated correctly; this proves the template door is now locked too.
"""
import datetime as dt
import json

from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, StaffProfile, User
from branches.models import Branch
from procurement.models import Procurement


class ProcurementTransitionMapTests(TestCase):
    def test_allowed_transitions_content(self):
        # Pin the exact map that was relocated from the DRF view.
        S = Procurement.Status
        self.assertEqual(Procurement.ALLOWED_TRANSITIONS, {
            S.DRAFT: {S.ORDERED, S.CANCELLED},
            S.ORDERED: {S.RECEIVED, S.CANCELLED},
            S.RECEIVED: set(),
            S.CANCELLED: set(),
        })

    def test_can_transition_to(self):
        S = Procurement.Status
        self.assertTrue(Procurement(status=S.DRAFT).can_transition_to(S.ORDERED))
        self.assertTrue(Procurement(status=S.DRAFT).can_transition_to(S.CANCELLED))
        self.assertTrue(Procurement(status=S.ORDERED).can_transition_to(S.RECEIVED))
        self.assertTrue(Procurement(status=S.ORDERED).can_transition_to(S.CANCELLED))
        self.assertFalse(Procurement(status=S.ORDERED).can_transition_to(S.DRAFT))
        self.assertFalse(Procurement(status=S.RECEIVED).can_transition_to(S.ORDERED))
        self.assertFalse(Procurement(status=S.CANCELLED).can_transition_to(S.ORDERED))
        self.assertFalse(Procurement(status=S.DRAFT).can_transition_to('Bogus'))
        self.assertFalse(Procurement(status=S.DRAFT).can_transition_to(None))


class ProcurementUpdateStatusViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='c3-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='C3 Branch', location='Baliuag', owner=cls.owner
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.owner)

    def _po(self, status):
        return Procurement.objects.create(
            branch=self.branch, supplier=None,
            order_date=dt.date(2026, 1, 1),
            status=status, total_cost=0,
        )

    def _post(self, po, target):
        return self.client.post(
            reverse('procurement_update_status', args=[po.pk]), {'status': target}
        )

    # -- legal transitions, one per (state -> target) map entry --------------

    def test_draft_to_ordered(self):
        po = self._po(Procurement.Status.DRAFT)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)
        self.assertRedirects(resp, reverse('procurement_detail', args=[po.pk]))

    def test_draft_to_cancelled(self):
        po = self._po(Procurement.Status.DRAFT)
        self._post(po, Procurement.Status.CANCELLED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.CANCELLED)

    def test_ordered_to_cancelled(self):
        po = self._po(Procurement.Status.ORDERED)
        self._post(po, Procurement.Status.CANCELLED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.CANCELLED)

    # -- illegal jumps, rejected by the TEMPLATE view (the door that was open)

    def test_received_cannot_go_back_to_ordered(self):
        po = self._po(Procurement.Status.RECEIVED)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.RECEIVED, 'unchanged')
        self.assertRedirects(resp, reverse('procurement_detail', args=[po.pk]))
        msgs = [m.message for m in resp.wsgi_request._messages]
        self.assertTrue(any('cannot change a Received order' in m for m in msgs), msgs)

    def test_received_cannot_go_to_cancelled(self):
        po = self._po(Procurement.Status.RECEIVED)
        self._post(po, Procurement.Status.CANCELLED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.RECEIVED)

    def test_cancelled_is_terminal(self):
        po = self._po(Procurement.Status.CANCELLED)
        for target in (Procurement.Status.ORDERED, Procurement.Status.DRAFT):
            self._post(po, target)
            po.refresh_from_db()
            self.assertEqual(po.status, Procurement.Status.CANCELLED)

    def test_ordered_cannot_go_back_to_draft(self):
        po = self._po(Procurement.Status.ORDERED)
        self._post(po, Procurement.Status.DRAFT)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)

    def test_arbitrary_string_is_rejected(self):
        po = self._po(Procurement.Status.DRAFT)
        resp = self._post(po, 'Shipped')
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        msgs = [m.message for m in resp.wsgi_request._messages]
        self.assertTrue(any('cannot change a Draft order' in m for m in msgs), msgs)

    # -- the Received-target special case is untouched ----------------------

    def test_received_target_redirects_to_delivery_without_writing_status(self):
        po = self._po(Procurement.Status.ORDERED)
        resp = self._post(po, Procurement.Status.RECEIVED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED, 'status NOT written here')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('delivery_receive', args=[po.pk]))

    def test_received_target_on_already_received_po_is_refused(self):
        po = self._po(Procurement.Status.RECEIVED)
        resp = self._post(po, Procurement.Status.RECEIVED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.RECEIVED)
        self.assertRedirects(resp, reverse('procurement_detail', args=[po.pk]))


class ProcurementStatusAPIParityTests(TestCase):
    """The DRF endpoint is now the second consumer of Procurement.ALLOWED_TRANSITIONS
    rather than owning its own copy — confirm the refactor kept it enforcing."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='c3-api-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='C3 API Branch', location='Baliuag', owner=cls.owner
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.owner)

    def _po(self, status):
        return Procurement.objects.create(
            branch=self.branch, supplier=None,
            order_date=dt.date(2026, 1, 1), status=status, total_cost=0,
        )

    def _patch(self, po, target):
        return self.client.patch(
            f'/api/procurement/{po.pk}/update_status/',
            data=json.dumps({'status': target}),
            content_type='application/json',
        )

    def test_legal_draft_to_ordered_ok(self):
        po = self._po(Procurement.Status.DRAFT)
        resp = self._patch(po, Procurement.Status.ORDERED)
        self.assertEqual(resp.status_code, 200)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)

    def test_illegal_cancelled_to_ordered_400(self):
        po = self._po(Procurement.Status.CANCELLED)
        resp = self._patch(po, Procurement.Status.ORDERED)
        self.assertEqual(resp.status_code, 400)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.CANCELLED)


class ProcurementUpdateStatusBranchScopeTests(TestCase):
    """C-6 — procurement_update_status (the template view) must gate on the
    caller's branch scope, the way delivery_receive and the DRF
    ProcurementStatusUpdateAPIView already do. Before this guard a Branch
    Manager could POST a status onto a PO belonging to a branch that wasn't
    theirs: an authorization gap, separate from C-3's transition-legality
    guard, and checked BEFORE it — a foreign-branch PO is refused even when
    the requested move (e.g. Draft -> Ordered) would be legal per C-3's map.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='c6-owner', password='pw', role=User.Role.OWNER
        )
        cls.home_branch = Branch.objects.create(
            branch_name='C6 Home', location='Baliuag', owner=cls.owner
        )
        cls.other_branch = Branch.objects.create(
            branch_name='C6 Other', location='Makinabang', owner=cls.owner
        )

        # A role-creation signal already makes the matching profile row, so
        # update_or_create to pin the branch (mirrors sales/tests.py).
        cls.manager = User.objects.create_user(
            username='c6-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        BranchManagerProfile.objects.update_or_create(
            user=cls.manager,
            defaults={'branch': cls.home_branch, 'manager_name': 'Mara',
                      'contact_number': '0900'},
        )
        cls.staff = User.objects.create_user(
            username='c6-staff', password='pw', role=User.Role.STAFF
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff,
            defaults={'branch': cls.home_branch, 'staff_name': 'Sam',
                      'contact_number': '0900'},
        )

    def setUp(self):
        self.client = Client()

    def _po(self, branch, status):
        return Procurement.objects.create(
            branch=branch, supplier=None,
            order_date=dt.date(2026, 1, 1), status=status, total_cost=0,
        )

    def _post(self, po, target):
        return self.client.post(
            reverse('procurement_update_status', args=[po.pk]), {'status': target}
        )

    # -- Owner: unrestricted, matching Owner scoping everywhere else ---------

    def test_owner_updates_status_on_any_branch(self):
        self.client.force_login(self.owner)
        po = self._po(self.other_branch, Procurement.Status.DRAFT)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)
        self.assertRedirects(resp, reverse('procurement_detail', args=[po.pk]))

    # -- Branch Manager: own branch works, same as C-3's transitions --------

    def test_branch_manager_updates_own_branch_po(self):
        self.client.force_login(self.manager)
        po = self._po(self.home_branch, Procurement.Status.DRAFT)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)
        self.assertRedirects(resp, reverse('procurement_detail', args=[po.pk]))

    # -- Branch Manager: a foreign branch is refused BEFORE the C-3 guard --

    def test_branch_manager_blocked_on_foreign_branch_legal_transition(self):
        """Draft -> Ordered is legal per C-3's map, but on someone else's
        branch it must still be refused, record untouched."""
        self.client.force_login(self.manager)
        po = self._po(self.other_branch, Procurement.Status.DRAFT)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT, 'unchanged')
        self.assertRedirects(resp, reverse('procurement_list'))
        msgs = [m.message for m in resp.wsgi_request._messages]
        self.assertTrue(
            any('belongs to another branch' in m for m in msgs), msgs
        )

    def test_branch_manager_foreign_branch_check_precedes_transition_check(self):
        """An illegal move (Received -> Ordered) on a foreign branch is
        rejected by the branch gate, not C-3's transition gate — the error
        names the branch, never the transition."""
        self.client.force_login(self.manager)
        po = self._po(self.other_branch, Procurement.Status.RECEIVED)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.RECEIVED, 'unchanged')
        self.assertRedirects(resp, reverse('procurement_list'))
        msgs = [m.message for m in resp.wsgi_request._messages]
        self.assertTrue(any('belongs to another branch' in m for m in msgs), msgs)
        self.assertFalse(
            any('cannot change a' in m for m in msgs),
            'C-3 transition message should never be reached',
        )

    # -- Staff: role-gated out of this view entirely (line ~325), so there
    #    is no branch-scope exposure via this door at all. Locked in here. --

    def test_staff_cannot_reach_status_update_on_own_branch(self):
        self.client.force_login(self.staff)
        po = self._po(self.home_branch, Procurement.Status.DRAFT)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT, 'unchanged')
        self.assertRedirects(
            resp, reverse('procurement_list'), fetch_redirect_response=False
        )
        msgs = [m.message for m in resp.wsgi_request._messages]
        self.assertTrue(
            any('do not have permission' in m for m in msgs), msgs
        )

    def test_staff_cannot_reach_status_update_on_foreign_branch(self):
        self.client.force_login(self.staff)
        po = self._po(self.other_branch, Procurement.Status.DRAFT)
        resp = self._post(po, Procurement.Status.ORDERED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT, 'unchanged')
        self.assertRedirects(
            resp, reverse('procurement_list'), fetch_redirect_response=False
        )
