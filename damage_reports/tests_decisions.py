"""Deciding a damage report: what approval does to stock, the withdrawal slip it
generates, the supplier-return flag, rejection, and above all that a report is
approved once and its units leave stock once.

    python manage.py test damage_reports.tests_decisions
"""
import threading
from datetime import timedelta

from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Notification, User
from audit.models import AuditLog
from damage_reports import services
from damage_reports.errors import DamageReportError, DamageReportForbidden
from damage_reports.models import DamageReport, DamageReportEvent
from damage_reports.testing import (
    FLOOR, WAREHOUSE, DamageFixtures, png,
)
from inventory.models import (
    Batch, Inventory, StockMovement, StockTransfer, StockWriteOff, WarehouseStock,
)

Status = DamageReport.Status
Resolution = DamageReport.Resolution
WRITE_OFF = Resolution.WRITE_OFF.value


class ApprovalStockTests(DamageFixtures, TestCase):
    """Approving a write-off takes the units out of the right stock, by batch,
    and writes the slip that records it."""

    def setUp(self):
        self.make_world()

    def approve(self, report, resolution=WRITE_OFF, actor=None, **kw):
        return services.approve_report(
            report.pk, actor or self.manager, resolution=resolution, **kw)

    def test_a_floor_write_off_deducts_the_named_batch_and_the_aggregate(self):
        report = self.file_report(quantity='5')

        approved = self.approve(report, note='Confirmed from the photos.')

        self.assertEqual(approved.status, Status.APPROVED)
        self.assertEqual(approved.resolution, WRITE_OFF)
        self.assertEqual(approved.reviewed_by, self.manager)
        self.assertIsNotNone(approved.reviewed_at)
        self.assertEqual(approved.resolution_notes, 'Confirmed from the photos.')
        self.assertEqual(self.on_hand(FLOOR), 25)
        self.assertEqual(self.batch_qty(self.floor_batch), 25)
        # Warehouse stock is untouched.
        self.assertEqual(self.on_hand(WAREHOUSE), 40)
        self.assertEqual(self.batch_qty(self.wh_batch), 40)

    def test_the_stock_movement_records_before_after_batch_and_actor(self):
        report = self.file_report(quantity='5')
        self.approve(report)
        report.refresh_from_db()

        move = StockMovement.objects.get()
        self.assertEqual(move.movement_type, StockMovement.MovementType.WRITE_OFF)
        self.assertEqual(move.location_type, StockMovement.LocationType.FLOOR)
        self.assertEqual((move.quantity_change, move.quantity_before, move.quantity_after),
                         (-5, 30, 25))
        self.assertEqual(move.batch, self.floor_batch)
        self.assertEqual(move.created_by, self.manager)
        self.assertEqual(move.reference_id, report.withdrawal_slip_id)
        self.assertIn(report.report_number, move.notes)

    def test_a_withdrawal_slip_is_generated_and_linked_both_ways(self):
        report = self.file_report(quantity='5')
        self.approve(report)
        report.refresh_from_db()

        slip = report.withdrawal_slip
        self.assertIsInstance(StockWriteOff.objects.get(pk=slip.pk), StockWriteOff)
        self.assertRegex(slip.slip_number, r'^WS-\d{4}-\d+$')
        self.assertEqual(slip.status, StockTransfer.Status.ACKNOWLEDGED)
        self.assertEqual(slip.purpose, StockTransfer.Purpose.DAMAGED_SPOILED)
        self.assertEqual(slip.source_branch, self.branch)
        self.assertIsNone(slip.destination_branch)
        self.assertEqual(slip.requested_by, self.staff)
        self.assertEqual(slip.approved_by, self.manager)
        self.assertEqual(slip.received_qty, 5)
        item = slip.items.get()
        self.assertEqual((item.product, item.quantity_requested, item.quantity_approved,
                          item.quantity_released), (self.product, 5, 5, 5))
        self.assertEqual(slip.damage_report, report)
        self.assertIsNotNone(report.stock_deducted_at)
        self.assertIn(report.report_number, slip.events.get().note)

    def test_the_slip_shows_up_in_the_write_off_list_and_links_back(self):
        report = self.file_report(quantity='5')
        self.approve(report)
        report.refresh_from_db()
        self.client.force_login(self.manager)

        listing = self.client.get(reverse('transfer_list'), {'view': 'writeoffs'})
        detail = self.client.get(reverse('transfer_detail', args=[report.withdrawal_slip_id]))

        self.assertContains(listing, report.withdrawal_slip.slip_number)
        self.assertContains(detail, report.report_number)
        self.assertContains(detail, reverse('damage_report_detail', args=[report.pk]))

    def test_a_warehouse_write_off_deducts_warehouse_stock(self):
        report = self.file_report(
            location=WAREHOUSE, batch_id=self.wh_batch.pk, quantity='12',
            damage_type='arrival')

        self.approve(report)

        self.assertEqual(self.on_hand(WAREHOUSE), 28)
        self.assertEqual(self.batch_qty(self.wh_batch), 28)
        self.assertEqual(self.on_hand(FLOOR), 30)
        move = StockMovement.objects.get()
        self.assertEqual(move.location_type, StockMovement.LocationType.WAREHOUSE)
        self.assertEqual(move.warehouse_stock, WarehouseStock.objects.get(
            warehouse__branch=self.branch, product=self.product))
        self.assertIsNone(move.inventory)

    def test_with_no_batch_named_the_oldest_expiry_goes_first_and_spans_lots(self):
        today = timezone.localdate()
        expired = self.add_stock(self.branch, self.product, FLOOR, 4, today - timedelta(days=3))
        soon = self.add_stock(self.branch, self.product, FLOOR, 10, today + timedelta(days=20))
        report = self.file_report(batch_id='', quantity='16')

        self.approve(report)

        # 4 expired + 10 soon + 2 from the long-dated lot.
        self.assertEqual(self.batch_qty(expired), 0)
        self.assertEqual(self.batch_qty(soon), 0)
        self.assertEqual(self.batch_qty(self.floor_batch), 28)
        self.assertEqual(self.on_hand(FLOOR), 28)   # 30 + 4 + 10 on hand, less 16
        moves = StockMovement.objects.order_by('pk')
        self.assertEqual([m.batch_id for m in moves], [expired.pk, soon.pk, self.floor_batch.pk])
        self.assertEqual([m.quantity_change for m in moves], [-4, -10, -2])
        # The before/after chain is continuous across the lots.
        self.assertEqual([(m.quantity_before, m.quantity_after) for m in moves],
                         [(44, 40), (40, 30), (30, 28)])

    def test_a_named_batch_is_the_only_one_touched(self):
        other = self.add_stock(
            self.branch, self.product, FLOOR, 10, timezone.localdate() + timedelta(days=5))
        report = self.file_report(quantity='3')

        self.approve(report)

        self.assertEqual(self.batch_qty(self.floor_batch), 27)
        self.assertEqual(self.batch_qty(other), 10)

    def test_approval_is_refused_when_the_stock_is_no_longer_there(self):
        report = self.file_report(quantity='20')
        # Sold in the meantime.
        Inventory.objects.filter(branch=self.branch, product=self.product).update(quantity_on_hand=10)
        Batch.objects.filter(pk=self.floor_batch.pk).update(quantity=10)

        with self.assertRaises(DamageReportError) as ctx:
            self.approve(report)

        self.assertIn('Only 10 units', str(ctx.exception))
        report.refresh_from_db()
        self.assertEqual(report.status, Status.PENDING)
        self.assertIsNone(report.withdrawal_slip)
        self.assertEqual(self.on_hand(FLOOR), 10)
        self.assertFalse(StockWriteOff.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_approval_is_refused_when_the_named_batch_ran_out_even_if_others_have_stock(self):
        report = self.file_report(quantity='20')
        Batch.objects.filter(pk=self.floor_batch.pk).update(quantity=5)
        self.add_stock(self.branch, self.product, FLOOR, 40)   # plenty of aggregate stock

        with self.assertRaises(DamageReportError) as ctx:
            self.approve(report)

        self.assertIn(self.floor_batch.batch_number, str(ctx.exception))
        report.refresh_from_db()
        self.assertEqual(report.status, Status.PENDING)

    def test_a_failure_part_way_leaves_nothing_behind(self):
        report = self.file_report(quantity='5')
        # No warehouse row for the slip: the deduction must not survive alone.
        from inventory.models import Warehouse
        Warehouse.objects.filter(branch=self.branch).delete()

        with self.assertRaises(DamageReportError):
            self.approve(report)

        self.assertEqual(self.on_hand(FLOOR), 30)
        self.assertEqual(self.batch_qty(self.floor_batch), 30)
        report.refresh_from_db()
        self.assertEqual(report.status, Status.PENDING)

    def test_approval_writes_the_history_and_tells_the_reporter(self):
        report = self.file_report(quantity='5')
        self.approve(report, note='ok')

        events = list(DamageReportEvent.objects.filter(report=report).order_by('pk'))
        self.assertEqual([e.to_status for e in events], [Status.PENDING, Status.APPROVED])
        self.assertEqual(events[-1].actor, self.manager)
        self.assertIn('withdrawal slip', events[-1].note)
        note = Notification.objects.get(user=self.staff, event_type='damage_report_status')
        self.assertIn('approved', note.title)
        self.assertEqual(note.action_url, reverse('damage_report_detail', args=[report.pk]))
        self.assertEqual(note.reference_id, str(report.pk))

    def test_a_manager_deciding_their_own_report_is_not_notified(self):
        report = self.file_report(user=self.manager, quantity='1')
        self.approve(report)
        self.assertFalse(Notification.objects.filter(
            user=self.manager, event_type='damage_report_status').exists())


class NoDoubleDeductionTests(DamageFixtures, TestCase):
    """A report is approved once. A second try, from any direction, moves nothing."""

    def setUp(self):
        self.make_world()
        self.report = self.file_report(quantity='5')

    def approve(self, actor=None):
        return services.approve_report(
            self.report.pk, actor or self.manager, resolution=WRITE_OFF)

    def assertDeductedOnce(self):
        self.assertEqual(self.on_hand(FLOOR), 25)
        self.assertEqual(self.batch_qty(self.floor_batch), 25)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockWriteOff.objects.count(), 1)

    def test_approving_twice_deducts_once(self):
        self.approve()
        with self.assertRaises(DamageReportError) as ctx:
            self.approve()
        self.assertIn('already approved', str(ctx.exception))
        self.assertDeductedOnce()

    def test_a_second_manager_approving_the_same_report_deducts_nothing(self):
        self.approve()
        second = self._manager('dr-manager-b', self.branch)
        for actor in (second, self.owner):
            with self.assertRaises(DamageReportError) as ctx:
                self.approve(actor)
            self.assertIn('already approved', str(ctx.exception))
        self.assertDeductedOnce()

    def test_approval_after_resolution_deducts_nothing_more(self):
        self.approve()
        services.resolve_report(self.report.pk, self.manager)
        with self.assertRaises(DamageReportError):
            self.approve()
        self.assertDeductedOnce()

    def test_a_different_resolution_cannot_deduct_again(self):
        self.approve()
        for resolution in (WRITE_OFF, Resolution.NONE.value):
            with self.assertRaises(DamageReportError):
                services.approve_report(self.report.pk, self.manager, resolution=resolution)
        self.assertDeductedOnce()

    def test_a_rejected_report_cannot_then_be_approved(self):
        services.reject_report(self.report.pk, self.manager, reason='Not damaged.')
        with self.assertRaises(DamageReportError):
            self.approve()
        self.assertEqual(self.on_hand(FLOOR), 30)
        self.assertFalse(StockWriteOff.objects.exists())

    def test_an_approved_report_cannot_then_be_rejected(self):
        self.approve()
        with self.assertRaises(DamageReportError):
            services.reject_report(self.report.pk, self.manager, reason='Changed my mind.')
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.APPROVED)
        self.assertDeductedOnce()

    def test_the_deduction_marker_blocks_a_second_deduction_even_past_the_status_check(self):
        """Defence in depth: should anything ever skip the status check, the
        slip link / timestamp still refuse a second deduction."""
        self.approve()
        self.report.refresh_from_db()
        self.assertTrue(self.report.stock_deducted_at)
        with self.assertRaises(DamageReportError) as ctx:
            from django.db import transaction
            with transaction.atomic():
                services._deduct_stock(self.report, self.manager, Resolution.WRITE_OFF)
        self.assertIn('already deducted', str(ctx.exception))
        self.assertDeductedOnce()

    def test_the_slip_link_is_one_to_one_so_a_report_cannot_own_two_slips(self):
        from django.db import IntegrityError, transaction
        self.approve()
        self.report.refresh_from_db()
        other = self.file_report(quantity='1', description='Another.')
        other.withdrawal_slip = self.report.withdrawal_slip
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                other.save()

    def test_the_same_form_posted_twice_through_the_view_deducts_once(self):
        self.client.force_login(self.manager)
        url = reverse('damage_report_approve', args=[self.report.pk])
        first = self.client.post(url, {'resolution': WRITE_OFF})
        second = self.client.post(url, {'resolution': WRITE_OFF}, follow=True)

        self.assertEqual(first.status_code, 302)
        messages = [str(m) for m in second.context['messages']]
        self.assertTrue(any('already approved' in m for m in messages), messages)
        self.assertDeductedOnce()


class ConcurrentApprovalTests(DamageFixtures, TransactionTestCase):
    """Two managers pressing Approve at the same instant."""

    def setUp(self):
        self.make_world()
        self.report = self.file_report(quantity='5')

    def test_only_one_of_two_simultaneous_approvals_succeeds(self):
        results = []
        barrier = threading.Barrier(2)

        def worker(actor_pk):
            try:
                barrier.wait(timeout=10)
                actor = User.objects.get(pk=actor_pk)
                services.approve_report(self.report.pk, actor, resolution=WRITE_OFF)
                results.append('ok')
            except DamageReportError as exc:
                results.append(f'refused: {exc}')
            except Exception as exc:                      # a deadlock would surface here
                results.append(f'error: {exc!r}')
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(pk,))
            for pk in (self.manager.pk, self.owner.pk)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(sorted(r.split(':')[0] for r in results), ['ok', 'refused'], results)
        self.assertEqual(self.on_hand(FLOOR), 25)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockWriteOff.objects.count(), 1)


class StatusMachineTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()

    def test_review_then_approve_then_resolve(self):
        report = self.file_report()

        reviewed = services.start_review(report.pk, self.manager)
        self.assertEqual(reviewed.status, Status.UNDER_REVIEW)
        # Still holds its units while under review.
        with self.assertRaises(DamageReportError):
            self.file_report(quantity='26')

        services.approve_report(report.pk, self.manager, resolution=WRITE_OFF)
        resolved = services.resolve_report(report.pk, self.manager, note='Disposed of.')

        self.assertEqual(resolved.status, Status.RESOLVED)
        self.assertIn('Disposed of.', resolved.resolution_notes)
        statuses = list(report.events.values_list('to_status', flat=True))
        self.assertEqual(statuses, [
            Status.PENDING, Status.UNDER_REVIEW, Status.APPROVED, Status.RESOLVED])

    def test_a_pending_report_can_be_decided_without_a_review_step(self):
        report = self.file_report()
        self.assertEqual(
            services.reject_report(report.pk, self.manager, reason='No.').status,
            Status.REJECTED)

    def test_illegal_moves_are_refused(self):
        pending = self.file_report()
        approved = self.file_report(quantity='1', description='B')
        services.approve_report(approved.pk, self.manager, resolution=WRITE_OFF)
        rejected = self.file_report(quantity='1', description='C')
        services.reject_report(rejected.pk, self.manager, reason='No.')
        resolved = self.file_report(quantity='1', description='D')
        services.approve_report(resolved.pk, self.manager, resolution=Resolution.NONE.value)
        services.resolve_report(resolved.pk, self.manager)

        cases = [
            ('resolve a pending report', lambda: services.resolve_report(pending.pk, self.manager)),
            ('review an approved report', lambda: services.start_review(approved.pk, self.manager)),
            ('review a rejected report', lambda: services.start_review(rejected.pk, self.manager)),
            ('review twice', lambda: (services.start_review(pending.pk, self.manager),
                                      services.start_review(pending.pk, self.manager))),
            ('resolve a rejected report', lambda: services.resolve_report(rejected.pk, self.manager)),
            ('reject a resolved report', lambda: services.reject_report(resolved.pk, self.manager, reason='x')),
            ('resolve twice', lambda: services.resolve_report(resolved.pk, self.manager)),
        ]
        for label, action in cases:
            with self.subTest(label):
                with self.assertRaises(DamageReportError):
                    action()

    def test_rejection_needs_a_reason(self):
        report = self.file_report()
        for reason in ('', '   ', 'x' * 501):
            with self.subTest(length=len(reason)):
                with self.assertRaises(DamageReportError):
                    services.reject_report(report.pk, self.manager, reason=reason)
        self.assertEqual(DamageReport.objects.get().status, Status.PENDING)

    def test_an_unknown_or_missing_resolution_is_refused(self):
        report = self.file_report()
        for resolution in ('', 'banana', Resolution.REFUND.value, Resolution.REPLACEMENT.value):
            with self.subTest(resolution=resolution):
                with self.assertRaises(DamageReportError):
                    services.approve_report(report.pk, self.manager, resolution=resolution)
        self.assertEqual(self.on_hand(FLOOR), 30)


class RejectionTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.report = self.file_report(photos=[png('a.png'), png('b.png')])

    def test_rejecting_moves_nothing_and_keeps_every_photo(self):
        photo_names = list(self.report.photos.values_list('image', flat=True))

        services.reject_report(self.report.pk, self.manager, reason='Looks undamaged.')

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.REJECTED)
        self.assertEqual(self.report.resolution, Resolution.NONE)
        self.assertEqual(self.report.resolution_notes, 'Looks undamaged.')
        self.assertEqual(self.report.reviewed_by, self.manager)
        self.assertEqual(self.on_hand(FLOOR), 30)
        self.assertFalse(StockWriteOff.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        # Evidence stays: rows and files.
        self.assertEqual(list(self.report.photos.values_list('image', flat=True)), photo_names)
        for photo in self.report.photos.all():
            with photo.image.open('rb') as handle:
                self.assertTrue(handle.read(8).startswith(b'\x89PNG'))

    def test_a_rejected_report_stays_viewable_with_its_photos(self):
        services.reject_report(self.report.pk, self.manager, reason='Looks undamaged.')
        self.client.force_login(self.manager)
        page = self.client.get(reverse('damage_report_detail', args=[self.report.pk]))
        self.assertContains(page, 'Looks undamaged.')
        for photo in self.report.photos.all():
            url = reverse('damage_report_photo', args=[self.report.pk, photo.pk])
            self.assertContains(page, url)
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_reporter_is_told_with_the_reason(self):
        services.reject_report(self.report.pk, self.manager, reason='Looks undamaged.')
        note = Notification.objects.get(user=self.staff, event_type='damage_report_status')
        self.assertIn('rejected', note.title)
        self.assertIn('Looks undamaged.', note.message)
        self.assertEqual(note.priority, 'medium')



class ReturnToSupplierTests(DamageFixtures, TestCase):
    """Supplier-caused damage found at receiving is flagged against the PO,
    and the supplier return - not this report - takes the units out."""

    def setUp(self):
        self.make_world()
        self.po = self.make_po()
        self.lot = self.add_stock(
            self.branch, self.product, WAREHOUSE, 12, procurement=self.po)

    def arrival_report(self, **kw):
        args = dict(
            location=WAREHOUSE, batch_id=self.lot.pk, quantity='4',
            damage_type='arrival', procurement_id=self.po.pk)
        args.update(kw)
        return self.file_report(**args)

    def test_arrival_damage_offers_return_to_supplier_first(self):
        report = self.arrival_report()
        self.assertEqual(
            [r.value for r in services.allowed_resolutions(report)],
            ['return_to_supplier', 'write_off', 'none'])

    def test_approving_flags_the_goods_and_links_the_purchase_order_without_deducting(self):
        report = self.arrival_report()
        before = (self.on_hand(WAREHOUSE), self.batch_qty(self.lot))

        services.approve_report(
            report.pk, self.manager, resolution=Resolution.RETURN_TO_SUPPLIER.value)

        report.refresh_from_db()
        self.assertEqual(report.status, Status.APPROVED)
        self.assertTrue(report.flagged_for_supplier_return)
        self.assertEqual(report.procurement, self.po)
        self.assertIsNone(report.withdrawal_slip)
        self.assertIsNone(report.stock_deducted_at)
        self.assertEqual((self.on_hand(WAREHOUSE), self.batch_qty(self.lot)), before)
        self.assertFalse(StockMovement.objects.exists())

    def test_flagged_units_stay_spoken_for_until_the_report_is_closed(self):
        report = self.arrival_report(quantity='12')
        services.approve_report(
            report.pk, self.manager, resolution=Resolution.RETURN_TO_SUPPLIER.value)

        with self.assertRaises(DamageReportError):
            self.arrival_report(quantity='1', description='Again')

        services.resolve_report(report.pk, self.manager, note='Supplier collected them.')
        # Resolved reports no longer hold anything: all 52 on hand (40 + 12) are free.
        self.assertEqual(
            services.reportable_quantity(self.branch.pk, self.product.pk, WAREHOUSE), 52)

    def test_the_flag_shows_on_the_report_and_on_the_purchase_order(self):
        report = self.arrival_report()
        services.approve_report(
            report.pk, self.manager, resolution=Resolution.RETURN_TO_SUPPLIER.value)
        self.client.force_login(self.manager)

        detail = self.client.get(reverse('damage_report_detail', args=[report.pk]))
        po_page = self.client.get(reverse('procurement_detail', args=[self.po.pk]))

        self.assertContains(detail, 'Waiting to go back to the supplier')
        self.assertContains(detail, reverse('supplier_return_add', args=[self.po.pk]))
        self.assertContains(po_page, report.report_number)
        self.assertContains(po_page, 'Flagged for return')

    def test_it_is_not_offered_without_a_purchase_order_or_for_floor_stock(self):
        no_po = self.file_report(
            location=WAREHOUSE, batch_id=self.wh_batch.pk, quantity='2', damage_type='storage')
        floor = self.file_report(quantity='2', description='Floor.')
        for report in (no_po, floor):
            with self.subTest(report=report.report_number):
                self.assertNotIn('return_to_supplier',
                                 [r.value for r in services.allowed_resolutions(report)])
                with self.assertRaises(DamageReportError):
                    services.approve_report(
                        report.pk, self.manager,
                        resolution=Resolution.RETURN_TO_SUPPLIER.value)

    def test_the_manager_can_still_write_the_units_off_instead(self):
        report = self.arrival_report()
        services.approve_report(report.pk, self.manager, resolution=WRITE_OFF)
        self.assertEqual(self.batch_qty(self.lot), 8)
        report.refresh_from_db()
        self.assertFalse(report.flagged_for_supplier_return)


class NoActionResolutionTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()

    def test_none_records_the_damage_and_moves_nothing(self):
        report = self.file_report()
        services.approve_report(report.pk, self.manager, resolution=Resolution.NONE.value)
        report.refresh_from_db()
        self.assertEqual(report.status, Status.APPROVED)
        self.assertEqual(self.on_hand(FLOOR), 30)
        self.assertIsNone(report.withdrawal_slip)
        self.assertFalse(StockMovement.objects.exists())


class DecisionAccessTests(DamageFixtures, TestCase):
    """Only an Owner or the branch's Manager can decide; the server enforces it."""

    def setUp(self):
        self.make_world()
        self.report = self.file_report()

    def post(self, user, action, data=None):
        self.client.force_login(user)
        return self.client.post(
            reverse(f'damage_report_{action}', args=[self.report.pk]), data or {})

    def assertUntouched(self):
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.PENDING)
        self.assertEqual(self.on_hand(FLOOR), 30)
        self.assertFalse(StockWriteOff.objects.exists())

    ACTIONS = (
        ('review', {}),
        ('approve', {'resolution': WRITE_OFF}),
        ('reject', {'reason': 'No.'}),
        ('resolve', {}),
    )

    def test_staff_cannot_decide_even_their_own_report(self):
        for action, data in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(self.staff, action, data).status_code, 403)
        self.assertUntouched()

    def test_a_staff_member_granted_edit_still_cannot_decide(self):
        User.objects.filter(pk=self.staff.pk).update(
            module_actions={'damage_reports': ['add', 'edit']})
        staff = User.objects.get(pk=self.staff.pk)
        for action, data in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(staff, action, data).status_code, 403)
        self.assertUntouched()

    def test_another_branchs_manager_cannot_see_or_decide_it(self):
        for action, data in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(self.other_manager, action, data).status_code, 404)
        self.assertUntouched()

    def test_a_manager_without_the_edit_grant_is_refused(self):
        User.objects.filter(pk=self.manager.pk).update(
            module_actions={'damage_reports': ['add']})
        manager = User.objects.get(pk=self.manager.pk)
        for action, data in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(manager, action, data).status_code, 403)
        self.assertUntouched()

    def test_a_customer_cannot_decide(self):
        for action, data in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.post(self.customer, action, data).status_code, 403)
        self.assertUntouched()

    def test_decisions_are_post_only(self):
        self.client.force_login(self.manager)
        for action, _data in self.ACTIONS:
            with self.subTest(action=action):
                response = self.client.get(reverse(f'damage_report_{action}', args=[self.report.pk]))
                self.assertEqual(response.status_code, 405)
        self.assertUntouched()

    def test_the_branch_manager_can_approve(self):
        response = self.post(self.manager, 'approve', {'resolution': WRITE_OFF})
        self.assertEqual(response.status_code, 302)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.APPROVED)
        self.assertEqual(self.on_hand(FLOOR), 25)

    def test_the_owner_can_approve_any_branch(self):
        response = self.post(self.owner, 'approve', {'resolution': WRITE_OFF})
        self.assertEqual(response.status_code, 302)
        self.report.refresh_from_db()
        self.assertEqual(self.report.reviewed_by, self.owner)

    def test_the_service_refuses_the_same_actors_directly(self):
        for actor in (self.staff, self.customer):
            with self.assertRaises(DamageReportForbidden):
                services.approve_report(self.report.pk, actor, resolution=WRITE_OFF)
        with self.assertRaises(DamageReportForbidden):
            services.approve_report(self.report.pk, self.other_manager, resolution=WRITE_OFF)
        self.assertUntouched()

    def test_an_unassigned_manager_is_refused_not_given_every_branch(self):
        from accounts.models import BranchManagerProfile
        BranchManagerProfile.objects.filter(user=self.manager).update(branch=None)
        manager = User.objects.get(pk=self.manager.pk)
        with self.assertRaises(DamageReportForbidden):
            services.approve_report(self.report.pk, manager, resolution=WRITE_OFF)
        self.assertUntouched()


class AuditTests(DamageFixtures, TestCase):
    """Every status change leaves an Audit Log entry."""

    def setUp(self):
        self.make_world()
        self.report = self.file_report()
        self.client.force_login(self.manager)

    def entries(self):
        return AuditLog.objects.filter(
            entity='damage_reports.DamageReport', record_id=str(self.report.pk))

    def status_changes(self):
        return [
            (e.before.get('status'), e.after.get('status'))
            for e in self.entries().order_by('pk')
            if e.action == 'update' and e.after and 'status' in e.after
        ]

    def test_filing_is_logged(self):
        created = self.entries().get(action='create')
        self.assertEqual(created.after['status'], 'Pending')
        # The customer-supplied description is never copied into the log.
        self.assertEqual(created.after['description'], '[personal data]')

    def test_each_transition_is_logged_with_who_and_the_before_and_after(self):
        self.client.post(reverse('damage_report_review', args=[self.report.pk]))
        self.client.post(reverse('damage_report_approve', args=[self.report.pk]),
                         {'resolution': WRITE_OFF})
        self.client.post(reverse('damage_report_resolve', args=[self.report.pk]))

        self.assertEqual(self.status_changes(), [
            ('Pending', 'Under Review'),
            ('Under Review', 'Approved'),
            ('Approved', 'Resolved'),
        ])
        approved = self.entries().filter(after__status='Approved').first()
        self.assertEqual(approved.actor_id, self.manager.pk)
        self.assertEqual(approved.actor_role, 'BRANCH_MANAGER')
        self.assertEqual(approved.branch_id, self.branch.pk)
        self.assertEqual(approved.action_label, 'Approved a damage report')

    def test_rejection_is_logged(self):
        self.client.post(reverse('damage_report_reject', args=[self.report.pk]),
                         {'reason': 'Not damaged.'})
        self.assertEqual(self.status_changes(), [('Pending', 'Rejected')])

    def test_a_readable_summary_is_recorded_for_an_approval(self):
        self.client.post(reverse('damage_report_approve', args=[self.report.pk]),
                         {'resolution': WRITE_OFF})
        summaries = list(self.entries().values_list('summary', flat=True))
        self.assertTrue(any('Approved damage report' in s and 'withdrawal slip' in s
                            for s in summaries), summaries)

    def test_the_withdrawal_slip_it_generates_is_audited_too(self):
        self.client.post(reverse('damage_report_approve', args=[self.report.pk]),
                         {'resolution': WRITE_OFF})
        self.report.refresh_from_db()
        self.assertTrue(AuditLog.objects.filter(
            entity='inventory.StockTransfer',
            record_id=str(self.report.withdrawal_slip_id), action='create').exists())

    def test_a_refused_decision_leaves_no_status_entry(self):
        self.client.post(reverse('damage_report_approve', args=[self.report.pk]),
                         {'resolution': WRITE_OFF})
        count = self.entries().count()
        self.client.post(reverse('damage_report_approve', args=[self.report.pk]),
                         {'resolution': WRITE_OFF})
        self.assertEqual(self.entries().count(), count)


class DecisionInAutocommitTests(DamageFixtures, TransactionTestCase):
    """Approval takes row locks; the view has no wrapping transaction in
    production, so it is exercised here exactly as it runs there."""

    def setUp(self):
        self.make_world()

    def test_a_manager_can_approve_through_the_real_view(self):
        report = self.file_report(quantity='5')
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('damage_report_approve', args=[report.pk]), {'resolution': WRITE_OFF})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.on_hand(FLOOR), 25)

    def test_the_owner_can_approve_through_the_real_view(self):
        report = self.file_report(quantity='5')
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('damage_report_approve', args=[report.pk]), {'resolution': WRITE_OFF})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.on_hand(FLOOR), 25)

    def test_reject_and_review_work_through_the_real_view(self):
        report = self.file_report(quantity='5')
        self.client.force_login(self.manager)
        self.client.post(reverse('damage_report_review', args=[report.pk]))
        self.client.post(reverse('damage_report_reject', args=[report.pk]), {'reason': 'No.'})
        report.refresh_from_db()
        self.assertEqual(report.status, Status.REJECTED)
