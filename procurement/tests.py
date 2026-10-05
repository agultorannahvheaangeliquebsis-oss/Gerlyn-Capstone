"""C-3 — procurement_update_status (the template view) must enforce
Procurement.ALLOWED_TRANSITIONS, the same map the DRF endpoint uses.

Before this guard the template view did `proc.status = request.POST['status'];
proc.save()` with no check: a Received PO could be forced back to Ordered, or
set to an arbitrary string. The DRF ProcurementStatusUpdateAPIView already
validated correctly; this proves the template door is now locked too.
"""
import datetime as dt
import json
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, StaffProfile, User
from branches.models import Branch
from inventory.models import (
    Batch, Product, StockMovement, Warehouse, WarehouseStock,
)
from procurement.models import Procurement, ProcurementItem, SupplierProduct


class ProcurementTransitionMapTests(TestCase):
    def test_allowed_transitions_content(self):
        # Pin the exact map that was relocated from the DRF view.
        S = Procurement.Status
        self.assertEqual(Procurement.ALLOWED_TRANSITIONS, {
            S.DRAFT: {S.APPROVED, S.CANCELLED},
            S.APPROVED: {
                S.INCOMING, S.PARTIALLY_RECEIVED, S.RECEIVED, S.CANCELLED,
            },
            S.INCOMING: {
                S.PARTIALLY_RECEIVED, S.RECEIVED, S.CANCELLED,
            },
            S.PARTIALLY_RECEIVED: {S.RECEIVED, S.CANCELLED},
            S.RECEIVED: set(),
            S.CANCELLED: set(),
        })

    def test_can_transition_to(self):
        S = Procurement.Status
        self.assertTrue(Procurement(status=S.DRAFT).can_transition_to(S.ORDERED))
        self.assertTrue(Procurement(status=S.DRAFT).can_transition_to(S.CANCELLED))
        self.assertTrue(Procurement(status=S.ORDERED).can_transition_to(S.RECEIVED))
        self.assertTrue(Procurement(status=S.ORDERED).can_transition_to(S.CANCELLED))
        self.assertTrue(Procurement(status=S.APPROVED).can_transition_to(S.INCOMING))
        self.assertTrue(Procurement(status=S.INCOMING).can_transition_to(S.PARTIALLY_RECEIVED))
        self.assertTrue(Procurement(status=S.PARTIALLY_RECEIVED).can_transition_to(S.RECEIVED))
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
        # Was Draft -> Ordered when this test was written for C-6. C-1 made
        # that move the Owner's approval step, so it is no longer a Manager's
        # to make (see ProcurementApproveTests's back-door test). The point
        # here is the branch gate, not which transition — Draft -> Cancelled
        # is still a Manager's own to make and exercises the same gate.
        self.client.force_login(self.manager)
        po = self._po(self.home_branch, Procurement.Status.DRAFT)
        resp = self._post(po, Procurement.Status.CANCELLED)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.CANCELLED)
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


# =============================================================================
# C-1 — a purchase order must be authorized before it commits spend.
#
# procurement_add created EVERY PO at Ordered regardless of who submitted the
# form, so a Branch Manager committed spend against a supplier with no Owner
# ever in the loop — and `owner`, the authorizer FK, was left null, so there
# was no trace of the approval that never happened. Role decides it outright:
# a Manager-raised PO lands in Draft and waits for procurement_approve; an
# Owner-raised one is authorized in the same act, exactly as before.
# =============================================================================


class ProcurementAuthorizationBase(TestCase):
    """Shared cast: an Owner, a Manager pinned to the home branch, a second
    branch to test scope against, and a supplier to order from."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='c1-owner', password='pw', role=User.Role.OWNER
        )
        cls.home_branch = Branch.objects.create(
            branch_name='C1 Home', location='Baliuag', owner=cls.owner
        )
        cls.other_branch = Branch.objects.create(
            branch_name='C1 Other', location='Makinabang', owner=cls.owner
        )
        cls.manager = User.objects.create_user(
            username='c1-manager', password='pw', role=User.Role.BRANCH_MANAGER
        )
        BranchManagerProfile.objects.update_or_create(
            user=cls.manager,
            defaults={'branch': cls.home_branch, 'manager_name': 'Mara',
                      'contact_number': '0900'},
        )
        cls.supplier = User.objects.create_user(
            username='c1-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.product = Product.objects.create(
            sku_code='C1-SKU-1', product_name='Sack of Rice', unit_price=100,
        )
        SupplierProduct.objects.create(
            supplier=cls.supplier,
            product=cls.product,
            current_cost_price=Decimal('50.00'),
        )

    def setUp(self):
        self.client = Client()

    def _po(self, branch=None, status=Procurement.Status.DRAFT, **extra):
        return Procurement.objects.create(
            branch=branch or self.home_branch, supplier=self.supplier,
            order_date=dt.date(2026, 1, 1), status=status, total_cost=0,
            **extra
        )


class ProcurementAddAuthorizationTests(ProcurementAuthorizationBase):
    """Which status a newly raised PO lands in, and who is recorded on it."""

    def _add(self, branch):
        return self.client.post(reverse('procurement_add'), {
            'branch': branch.pk,
            'supplier': self.supplier.pk,
            'order_date': '2026-01-01',
            'product_id': [str(self.product.pk)],
            'quantity_ordered': ['10'],
            'cost_price': ['50'],
        })

    def test_manager_raised_po_lands_in_draft_unauthorized(self):
        self.client.force_login(self.manager)
        self._add(self.home_branch)

        po = Procurement.objects.get()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        # The two halves of "nobody has approved this": no authorizer, and no
        # approval timestamp. Before C-1 this row was Ordered with owner=None,
        # which read as an authorized order whose authorizer was just missing.
        self.assertIsNone(po.owner)
        self.assertIsNone(po.authorized_at)
        # ...but the raiser is recorded, so the draft is still attributable.
        self.assertEqual(po.created_by, self.manager)

    def test_owner_raised_po_is_ordered_and_authorized(self):
        """Regression: the Owner's path is unchanged behaviour, now explicit."""
        self.client.force_login(self.owner)
        self._add(self.home_branch)

        po = Procurement.objects.get()
        self.assertEqual(po.status, Procurement.Status.ORDERED)
        self.assertEqual(po.owner, self.owner)
        self.assertIsNotNone(po.authorized_at)
        self.assertEqual(po.created_by, self.owner)


class ProcurementApproveTests(ProcurementAuthorizationBase):
    """The Owner's authorization step, Draft -> Ordered."""

    def _approve(self, po):
        return self.client.post(reverse('procurement_approve', args=[po.pk]))

    def test_owner_approves_a_draft(self):
        po = self._po(status=Procurement.Status.DRAFT)
        self.client.force_login(self.owner)

        self._approve(po)

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)
        self.assertEqual(po.owner, self.owner)
        self.assertIsNotNone(po.authorized_at)

    def test_manager_cannot_approve(self):
        """The whole point of the gate: the raiser cannot authorize."""
        po = self._po(status=Procurement.Status.DRAFT)
        self.client.force_login(self.manager)

        self._approve(po)

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        self.assertIsNone(po.owner)
        self.assertIsNone(po.authorized_at)

    def test_already_ordered_po_cannot_be_approved_again(self):
        """Draft is the only status whose ALLOWED_TRANSITIONS include Ordered,
        so the C-3 map does this check — re-approving must not re-stamp
        owner/authorized_at onto an order that is already live."""
        original_owner = User.objects.create_user(
            username='c1-owner-2', password='pw', role=User.Role.OWNER
        )
        po = self._po(status=Procurement.Status.ORDERED, owner=original_owner)
        self.client.force_login(self.owner)

        self._approve(po)

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)
        self.assertEqual(po.owner, original_owner)
        self.assertIsNone(po.authorized_at)

    def test_received_po_cannot_be_approved(self):
        po = self._po(status=Procurement.Status.RECEIVED)
        self.client.force_login(self.owner)

        self._approve(po)

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.RECEIVED)
        self.assertIsNone(po.authorized_at)

    def test_get_does_not_approve(self):
        """Approval is a state change, so it must not ride on a bare GET."""
        po = self._po(status=Procurement.Status.DRAFT)
        self.client.force_login(self.owner)

        self.client.get(reverse('procurement_approve', args=[po.pk]))

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT)

    def test_manager_cannot_approve_via_update_status_back_door(self):
        """Draft -> Ordered is legal on the C-3 map and procurement_update_status
        accepts Branch Managers, so without an explicit check the gate would be
        one locked door beside an open one: a Manager could approve their own
        spend by picking 'Ordered' from the status form."""
        po = self._po(status=Procurement.Status.DRAFT)
        self.client.force_login(self.manager)

        self.client.post(
            reverse('procurement_update_status', args=[po.pk]),
            {'status': Procurement.Status.ORDERED},
        )

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        self.assertIsNone(po.owner)
        self.assertIsNone(po.authorized_at)

    def test_manager_can_still_cancel_a_draft(self):
        """The back-door guard is narrow: it blocks the approval move only, not
        a Manager withdrawing a draft they raised."""
        po = self._po(status=Procurement.Status.DRAFT)
        self.client.force_login(self.manager)

        self.client.post(
            reverse('procurement_update_status', args=[po.pk]),
            {'status': Procurement.Status.CANCELLED},
        )

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.CANCELLED)

    def test_owner_may_approve_a_draft_on_any_branch(self):
        """C-6's branch gate runs on this endpoint for consistency, but an
        Owner is unrestricted by branch_ids_for_user (returns None), so it
        must not narrow the Owner's reach. It is defence in depth here: with
        approval already Owner-only, no role that the gate would reject can
        get past the role check to reach it."""
        po = self._po(branch=self.other_branch, status=Procurement.Status.DRAFT)
        self.client.force_login(self.owner)

        self.client.post(reverse('procurement_approve', args=[po.pk]))

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.ORDERED)

    def test_manager_cannot_approve_a_foreign_branch_draft(self):
        """A Manager reaching for another branch's draft is refused, and the
        draft is untouched — the out-of-scope case C-6 cared about, though
        here it is the role check that turns them away first."""
        po = self._po(branch=self.other_branch, status=Procurement.Status.DRAFT)
        self.client.force_login(self.manager)

        self.client.post(reverse('procurement_approve', args=[po.pk]))

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        self.assertIsNone(po.owner)
        self.assertIsNone(po.authorized_at)


class DeliveryReceiveRequiresApprovalTests(ProcurementAuthorizationBase):
    """delivery_receive rejected only an already-Received or Cancelled PO, so a
    Draft one could be received: unapproved goods credited to warehouse stock.
    Flagged during C-6 as worth revisiting once C-1 made Draft a commonly
    reached state — this is that revisit."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Creating a Branch already fires branches/signals.py, which makes its
        # Warehouse — the FK is OneToOne, so create a second and the insert
        # fails. Take the one that exists.
        cls.warehouse, _ = Warehouse.objects.get_or_create(
            branch=cls.home_branch, defaults={'name': 'C1 Warehouse'}
        )

    def _po_with_item(self, status):
        po = self._po(status=status)
        item = ProcurementItem.objects.create(
            procurement=po, product=self.product,
            quantity_ordered=10, cost_price=50,
        )
        return po, item

    def _receive(self, po, item, qty=10):
        return self.client.post(
            reverse('delivery_receive', args=[po.pk]),
            {'item_id': [str(item.pk)], 'quantity_received': [str(qty)]},
            follow=True,
        )

    def test_draft_po_cannot_be_received_and_lands_no_stock(self):
        po, item = self._po_with_item(Procurement.Status.DRAFT)
        self.client.force_login(self.owner)

        self._receive(po, item)

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        # The finding is not that an error fired — it is that nothing landed.
        # Assert the absence of every row a real receipt would have written.
        self.assertFalse(
            WarehouseStock.objects.filter(product=self.product).exists(),
            'an unapproved PO must not credit warehouse stock',
        )
        self.assertFalse(
            Batch.objects.filter(product=self.product).exists(),
            'an unapproved PO must not create a batch',
        )
        self.assertFalse(
            StockMovement.objects.filter(
                reference_id=po.pk, movement_type='Procurement Received'
            ).exists(),
            'an unapproved PO must not be audited as received',
        )

    def test_draft_rejection_says_it_needs_approval(self):
        po, item = self._po_with_item(Procurement.Status.DRAFT)
        self.client.force_login(self.owner)

        resp = self._receive(po, item)

        text = ' '.join(str(m) for m in get_messages(resp.wsgi_request))
        self.assertIn('Draft', text)
        self.assertIn('approved', text)

    def test_ordered_po_still_receives(self):
        """Regression against the existing delivery behaviour: the new guard
        adds Draft to the rejected set and disturbs nothing else."""
        po, item = self._po_with_item(Procurement.Status.ORDERED)
        self.client.force_login(self.owner)

        self._receive(po, item)

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.RECEIVED)
        self.assertTrue(
            WarehouseStock.objects.filter(
                warehouse=self.warehouse, product=self.product
            ).exists(),
            'an approved receipt must still credit warehouse stock',
        )


class IncomingDeliveryWorkflowTests(ProcurementAuthorizationBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.warehouse, _ = Warehouse.objects.get_or_create(
            branch=cls.home_branch, defaults={'name': 'Incoming Warehouse'}
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.owner)
        self.po = self._po(
            status=Procurement.Status.INCOMING,
            delivery_date=dt.date(2026, 2, 10),
        )
        self.item = ProcurementItem.objects.create(
            procurement=self.po,
            product=self.product,
            quantity_ordered=10,
            cost_price=Decimal('50.00'),
        )

    def _receive(self, quantity, date):
        return self.client.post(
            reverse('delivery_receive', args=[self.po.pk]),
            {
                'item_id': [str(self.item.pk)],
                'quantity_received': [str(quantity)],
                'actual_receipt_date': date,
            },
        )

    def test_partial_then_final_receipt_tracks_dates_and_only_new_stock(self):
        self._receive(4, '2026-02-11')

        self.po.refresh_from_db()
        self.item.refresh_from_db()
        stock = WarehouseStock.objects.get(
            warehouse=self.warehouse, product=self.product
        )
        self.assertEqual(self.po.status, Procurement.Status.PARTIALLY_RECEIVED)
        self.assertEqual(self.po.receipt_flag, Procurement.ReceiptFlag.PARTIAL)
        self.assertEqual(str(self.po.actual_receipt_date), '2026-02-11')
        self.assertEqual(self.item.quantity_received, 4)
        self.assertEqual(stock.quantity_on_hand, 4)

        self._receive(6, '2026-02-14')

        self.po.refresh_from_db()
        self.item.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.RECEIVED)
        self.assertEqual(str(self.po.actual_receipt_date), '2026-02-14')
        self.assertEqual(self.item.quantity_received, 10)
        self.assertEqual(stock.quantity_on_hand, 10)
        self.assertEqual(
            StockMovement.objects.filter(reference_id=self.po.pk).count(), 2
        )

    def test_incoming_queue_keeps_partial_po_visible(self):
        self._receive(4, '2026-02-11')

        response = self.client.get(reverse('delivery_list'))

        self.assertContains(response, self.po.po_number)
        self.assertContains(response, 'Partially Received')

    def test_api_partial_receipt_returns_actual_status_and_date(self):
        response = self.client.patch(
            f'/api/procurement/{self.po.pk}/update_status/',
            data=json.dumps({
                'status': Procurement.Status.RECEIVED,
                'actualReceiptDate': '2026-02-11',
                'items': [{
                    'itemId': self.item.pk,
                    'quantityReceived': 4,
                }],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()['status'], Procurement.Status.PARTIALLY_RECEIVED
        )
        self.assertEqual(response.json()['actualReceiptDate'], '2026-02-11')


class SupplierPortalHidesDraftsTests(ProcurementAuthorizationBase):
    """A draft is an internal proposal, not a commitment to the supplier.

    supplier_portal listed every PO addressed to a supplier with no status
    filter, which was safe only because nothing reachable was ever Draft.
    C-1 makes Draft the starting state of every Manager-raised order, so
    without this the supplier would see unapproved orders as real ones.
    """

    def test_draft_is_not_listed_in_the_supplier_portal(self):
        draft = self._po(status=Procurement.Status.DRAFT)
        self.client.force_login(self.supplier)

        resp = self.client.get(reverse('supplier_portal'))

        self.assertNotContains(resp, draft.po_number)

    def test_approved_order_is_listed_in_the_supplier_portal(self):
        ordered = self._po(status=Procurement.Status.ORDERED)
        self.client.force_login(self.supplier)

        resp = self.client.get(reverse('supplier_portal'))

        self.assertContains(resp, ordered.po_number)

    def test_approving_a_draft_reveals_it_to_the_supplier(self):
        """The end-to-end shape of the gate: raised by a Manager, invisible to
        the supplier, then visible the moment the Owner approves it."""
        po = self._po(status=Procurement.Status.DRAFT)

        self.client.force_login(self.supplier)
        self.assertNotContains(
            self.client.get(reverse('supplier_portal')), po.po_number
        )

        self.client.force_login(self.owner)
        self.client.post(reverse('procurement_approve', args=[po.pk]))

        self.client.force_login(self.supplier)
        self.assertContains(
            self.client.get(reverse('supplier_portal')), po.po_number
        )


class SupplierOwnedCostTests(ProcurementAuthorizationBase):
    def _add(self, user, posted_cost='999.99'):
        self.client.force_login(user)
        return self.client.post(reverse('procurement_add'), {
            'branch': self.home_branch.pk,
            'supplier': self.supplier.pk,
            'order_date': '2026-01-01',
            'product_id': [str(self.product.pk)],
            'quantity_ordered': ['2'],
            # A stale or malicious client may still submit this legacy key;
            # it must never influence procurement cost.
            'cost_price': [posted_cost],
        })

    def test_owner_form_ignores_submitted_cost_and_snapshots_supplier_cost(self):
        self._add(self.owner)

        po = Procurement.objects.get()
        item = po.items.get()
        self.assertEqual(po.status, Procurement.Status.ORDERED)
        self.assertEqual(item.unit_cost_snapshot, Decimal('50.00'))
        self.assertEqual(po.total_cost, Decimal('100.00'))
        self.assertNotEqual(item.unit_cost_snapshot, Decimal('999.99'))
        self.assertEqual(self.product.unit_price, Decimal('100.00'))

    def test_manager_draft_snapshots_latest_supplier_cost_on_approval(self):
        self._add(self.manager)
        po = Procurement.objects.get()
        item = po.items.get()
        self.assertIsNone(item.unit_cost_snapshot)
        self.assertEqual(po.total_cost, Decimal('0.00'))

        link = SupplierProduct.objects.get(
            supplier=self.supplier, product=self.product
        )
        self.client.force_login(self.supplier)
        self.client.post(
            reverse('supplier_set_product_cost', args=[link.pk]),
            {'current_cost_price': '75.25'},
        )

        self.client.force_login(self.owner)
        self.client.post(reverse('procurement_approve', args=[po.pk]))
        po.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(item.unit_cost_snapshot, Decimal('75.25'))
        self.assertEqual(po.total_cost, Decimal('150.50'))

        link.current_cost_price = Decimal('80.00')
        link.save(update_fields=['current_cost_price'])
        item.refresh_from_db()
        self.assertEqual(item.unit_cost_snapshot, Decimal('75.25'))

    def test_missing_supplier_cost_blocks_confirmation(self):
        link = SupplierProduct.objects.get(
            supplier=self.supplier, product=self.product
        )
        link.current_cost_price = None
        link.save(update_fields=['current_cost_price'])
        self._add(self.manager)
        po = Procurement.objects.get()

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('procurement_approve', args=[po.pk])
        )

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        self.assertIsNone(po.items.get().unit_cost_snapshot)
        messages_text = ' '.join(
            str(message) for message in get_messages(response.wsgi_request)
        )
        self.assertIn('must set a current cost', messages_text)

    def test_only_owning_supplier_can_change_current_cost(self):
        other = User.objects.create_user(
            username='other-cost-supplier', password='pw',
            role=User.Role.SUPPLIER,
        )
        link = SupplierProduct.objects.get(
            supplier=self.supplier, product=self.product
        )
        self.client.force_login(other)

        response = self.client.post(
            reverse('supplier_set_product_cost', args=[link.pk]),
            {'current_cost_price': '1.00'},
        )

        self.assertEqual(response.status_code, 404)
        link.refresh_from_db()
        self.assertEqual(link.current_cost_price, Decimal('50.00'))

    def test_internal_po_form_has_no_editable_cost_field(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_list'))
        self.assertNotContains(response, 'name="cost_price"')
        self.assertContains(response, 'Supplier Unit Cost')

    def test_api_snapshots_supplier_cost_instead_of_submitted_cost(self):
        self.client.force_login(self.owner)
        created = self.client.post(
            '/api/procurement/',
            data=json.dumps({
                'branchId': self.home_branch.pk,
                'supplierId': self.supplier.pk,
                'orderDate': '2026-01-01',
                'items': [{
                    'productId': self.product.pk,
                    'quantityOrdered': 3,
                    'costPrice': '999.99',
                }],
            }),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 201, created.content)
        po = Procurement.objects.get()
        self.assertIsNone(po.items.get().unit_cost_snapshot)

        confirmed = self.client.patch(
            f'/api/procurement/{po.pk}/update_status/',
            data=json.dumps({'status': Procurement.Status.ORDERED}),
            content_type='application/json',
        )

        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        po.refresh_from_db()
        item = po.items.get()
        self.assertEqual(item.unit_cost_snapshot, Decimal('50.00'))
        self.assertEqual(po.total_cost, Decimal('150.00'))
        self.assertEqual(confirmed.json()['items'][0]['unitCostSnapshot'], '50.00')
        self.assertNotIn('costPrice', confirmed.json()['items'][0])


class SupplierCostCeilingTests(ProcurementAuthorizationBase):
    """current_cost_price must stay within core.validation.MAX_PRICE —
    same ₱250,000 ceiling as a product's own retail price."""

    def test_cost_over_the_ceiling_is_rejected(self):
        link = SupplierProduct.objects.get(
            supplier=self.supplier, product=self.product
        )
        self.client.force_login(self.supplier)
        response = self.client.post(
            reverse('supplier_set_product_cost', args=[link.pk]),
            {'current_cost_price': '250000.01'},
        )
        self.assertRedirects(response, reverse('supplier_portal'))
        link.refresh_from_db()
        self.assertEqual(link.current_cost_price, Decimal('50.00'))
        messages_text = ' '.join(
            str(message) for message in get_messages(response.wsgi_request)
        )
        self.assertIn('cannot exceed', messages_text)

    def test_cost_at_the_ceiling_is_accepted(self):
        link = SupplierProduct.objects.get(
            supplier=self.supplier, product=self.product
        )
        self.client.force_login(self.supplier)
        self.client.post(
            reverse('supplier_set_product_cost', args=[link.pk]),
            {'current_cost_price': '250000'},
        )
        link.refresh_from_db()
        self.assertEqual(link.current_cost_price, Decimal('250000'))
