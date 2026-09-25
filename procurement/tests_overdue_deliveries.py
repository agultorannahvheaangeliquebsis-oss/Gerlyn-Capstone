"""P18 — overdue purchase-order deliveries: a daily, idempotent sweep that
flags and notifies (never cancels), plus a manual reschedule."""
import datetime as dt
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import BranchManagerProfile, Notification, StaffProfile, User
from branches.models import Branch
from procurement.models import Procurement
from procurement.services import (
    RescheduleError, overdue_orders, process_overdue_deliveries, reschedule_delivery,
)

P = Procurement.Status
TODAY = dt.date(2026, 6, 10)


class OverdueBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='p18-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='P18 Home', location='Baliuag', owner=cls.owner)
        cls.other_branch = Branch.objects.create(
            branch_name='P18 Other', location='Makinabang', owner=cls.owner)
        cls.manager = User.objects.create_user(
            username='p18-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=cls.manager,
            defaults={'branch': cls.branch, 'manager_name': 'M', 'contact_number': '0'})
        cls.other_manager = User.objects.create_user(
            username='p18-mgr2', password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=cls.other_manager,
            defaults={'branch': cls.other_branch, 'manager_name': 'M2', 'contact_number': '0'})
        cls.supplier = User.objects.create_user(
            username='p18-supplier', password='pw', role=User.Role.SUPPLIER)

    def po(self, days_late, status=P.INCOMING, branch=None, **extra):
        return Procurement.objects.create(
            branch=branch or self.branch, supplier=self.supplier,
            order_date=TODAY - dt.timedelta(days=30), status=status, total_cost=0,
            delivery_date=TODAY - dt.timedelta(days=days_late), **extra)

    def notes(self, event, user=None):
        qs = Notification.objects.filter(event_type=event)
        return qs.filter(user=user) if user else qs


class SweepTests(OverdueBase):
    def test_not_yet_late_or_on_the_day_is_left_alone(self):
        self.po(0)
        self.po(-3)  # due in the future
        result = process_overdue_deliveries(today=TODAY)
        self.assertEqual((result.flagged, result.escalated), (0, 0))
        self.assertFalse(Notification.objects.exists())

    def test_one_day_late_notifies_owner_manager_and_supplier_once(self):
        po = self.po(1)
        result = process_overdue_deliveries(today=TODAY)
        self.assertEqual((result.flagged, result.escalated), (1, 0))
        for user in (self.owner, self.manager, self.supplier):
            self.assertEqual(self.notes('po_overdue', user).count(), 1, user)
        # A different branch's manager is not told.
        self.assertFalse(self.notes('po_overdue', self.other_manager).exists())
        po.refresh_from_db()
        self.assertIsNotNone(po.overdue_flagged_at)
        self.assertIsNone(po.overdue_escalated_at)
        self.assertEqual(po.status, P.INCOMING)  # never cancelled

    def test_the_sweep_is_idempotent(self):
        self.po(2)
        process_overdue_deliveries(today=TODAY)
        again = process_overdue_deliveries(today=TODAY)
        self.assertEqual((again.flagged, again.escalated), (0, 0))
        self.assertEqual(self.notes('po_overdue').count(), 3)

    def test_three_days_late_escalates_to_the_owner_only_once(self):
        po = self.po(3)
        result = process_overdue_deliveries(today=TODAY)
        self.assertEqual((result.flagged, result.escalated), (1, 1))
        self.assertEqual(self.notes('po_overdue_escalated').count(), 1)
        self.assertEqual(self.notes('po_overdue_escalated', self.owner).count(), 1)
        escalation = self.notes('po_overdue_escalated', self.owner).get()
        self.assertEqual(escalation.priority, 'critical')
        self.assertTrue(escalation.requires_action)
        po.refresh_from_db()
        self.assertIsNotNone(po.overdue_escalated_at)
        process_overdue_deliveries(today=TODAY)
        self.assertEqual(self.notes('po_overdue_escalated').count(), 1)

    def test_a_po_late_by_one_day_then_by_three_escalates_on_the_later_run(self):
        self.po(1)
        process_overdue_deliveries(today=TODAY)
        later = process_overdue_deliveries(today=TODAY + dt.timedelta(days=2))
        self.assertEqual((later.flagged, later.escalated), (0, 1))
        self.assertEqual(self.notes('po_overdue').count(), 3)

    def test_only_awaiting_delivery_statuses_are_swept(self):
        for status in (P.DRAFT, P.APPROVED, P.PENDING_OWNER_REVIEW, P.TERMS_AGREED,
                       P.RECEIVED, P.CANCELLED):
            self.po(5, status=status)
        result = process_overdue_deliveries(today=TODAY)
        self.assertEqual(result.flagged, 0)

    def test_partially_received_orders_are_swept_too(self):
        self.po(2, status=P.PARTIALLY_RECEIVED)
        self.assertEqual(process_overdue_deliveries(today=TODAY).flagged, 1)

    def test_supplier_message_links_to_their_portal(self):
        self.po(1)
        process_overdue_deliveries(today=TODAY)
        note = self.notes('po_overdue', self.supplier).get()
        self.assertEqual(note.action_url, reverse('supplier_portal'))
        self.assertFalse(note.requires_action)

    @override_settings(PO_OVERDUE_GRACE_DAYS=2, PO_OVERDUE_ESCALATE_DAYS=5)
    def test_thresholds_come_from_settings(self):
        self.po(1)
        self.assertEqual(process_overdue_deliveries(today=TODAY).flagged, 0)
        self.po(2, branch=self.branch)
        self.assertEqual(process_overdue_deliveries(today=TODAY).flagged, 1)
        self.po(5)
        self.assertEqual(process_overdue_deliveries(today=TODAY).escalated, 1)

    def test_received_between_listing_and_locking_is_skipped(self):
        po = self.po(2)
        Procurement.objects.filter(pk=po.pk).update(status=P.RECEIVED)
        self.assertEqual(process_overdue_deliveries(today=TODAY).flagged, 0)


class CommandTests(OverdueBase):
    def test_command_reports_and_dispatch_runs_it(self):
        Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, status=P.INCOMING,
            order_date=TODAY, total_cost=0,
            delivery_date=timezone.localdate() - dt.timedelta(days=2))
        out = StringIO()
        call_command('process_overdue_deliveries', stdout=out)
        self.assertIn('Flagged 1 overdue delivery', out.getvalue())

        Procurement.objects.update(overdue_flagged_at=None)
        out = StringIO()
        call_command('dispatch_notifications', stdout=out)
        self.assertIn('Flagged 1 overdue delivery', out.getvalue())


class RescheduleTests(OverdueBase):
    def _post(self, user, po, date):
        self.client.force_login(user)
        return self.client.post(
            reverse('procurement_reschedule', args=[po.pk]), {'delivery_date': date})

    def _future(self, days=5):
        return timezone.localdate() + dt.timedelta(days=days)

    def test_reschedule_moves_the_date_clears_flags_and_tells_the_supplier(self):
        po = self.po(3)
        process_overdue_deliveries(today=TODAY)
        new = self._future()
        self._post(self.owner, po, new.isoformat())
        po.refresh_from_db()
        self.assertEqual(po.delivery_date, new)
        self.assertIsNone(po.overdue_flagged_at)
        self.assertIsNone(po.overdue_escalated_at)
        self.assertEqual(po.status, P.INCOMING)
        self.assertTrue(Notification.objects.filter(
            user=self.supplier, title__contains='new delivery date').exists())

    def test_a_rescheduled_order_can_go_overdue_again(self):
        po = self.po(3)
        process_overdue_deliveries(today=TODAY)
        reschedule_delivery(po, self._future(), today=timezone.localdate())
        later = timezone.localdate() + dt.timedelta(days=10)
        result = process_overdue_deliveries(today=later)
        self.assertEqual(result.flagged, 1)

    def test_past_or_missing_dates_are_refused(self):
        po = self.po(3)
        yesterday = (timezone.localdate() - dt.timedelta(days=1)).isoformat()
        for bad in (yesterday, '', 'not-a-date'):
            self._post(self.owner, po, bad)
        po.refresh_from_db()
        self.assertEqual(po.delivery_date, TODAY - dt.timedelta(days=3))

    def test_only_awaiting_delivery_orders_can_be_rescheduled(self):
        for status in (P.APPROVED, P.RECEIVED, P.CANCELLED):
            po = self.po(3, status=status)
            with self.assertRaises(RescheduleError):
                reschedule_delivery(po, self._future())

    def test_branch_scope_and_roles(self):
        po = self.po(3)
        new = self._future().isoformat()
        self._post(self.other_manager, po, new)   # another branch
        self._post(self.supplier, po, new)        # a supplier
        staff = User.objects.create_user(
            username='p18-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=staff, defaults={'branch': self.branch, 'staff_name': 'S', 'contact_number': '0'})
        self._post(staff, po, new)                # staff may not
        po.refresh_from_db()
        self.assertEqual(po.delivery_date, TODAY - dt.timedelta(days=3))
        self._post(self.manager, po, new)         # own branch manager may
        po.refresh_from_db()
        self.assertEqual(po.delivery_date.isoformat(), new)

    def test_get_is_not_allowed(self):
        po = self.po(3)
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('procurement_reschedule', args=[po.pk])).status_code, 405)


class OverdueDisplayTests(OverdueBase):
    def _late(self, days):
        today = timezone.localdate()
        return Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, status=P.INCOMING,
            order_date=today - dt.timedelta(days=30), total_cost=0,
            delivery_date=today - dt.timedelta(days=days))

    def test_days_overdue_property(self):
        po = self._late(4)
        self.assertEqual(po.days_overdue(), 4)
        po.status = P.RECEIVED
        self.assertEqual(po.days_overdue(), 0)
        po.status = P.INCOMING
        po.delivery_date = None
        self.assertEqual(po.days_overdue(), 0)

    def test_detail_page_warns_and_offers_reschedule(self):
        po = self._late(2)
        self.client.force_login(self.owner)
        page = self.client.get(reverse('procurement_detail', args=[po.pk]))
        self.assertContains(page, '2 days overdue')
        self.assertContains(page, 'New delivery date')

    def test_detail_page_has_no_reschedule_for_a_received_order(self):
        po = self._late(2)
        Procurement.objects.filter(pk=po.pk).update(status=P.RECEIVED)
        self.client.force_login(self.owner)
        page = self.client.get(reverse('procurement_detail', args=[po.pk]))
        self.assertNotContains(page, 'New delivery date')
        self.assertNotContains(page, 'days overdue')

    def test_delivery_list_shows_days_late(self):
        self._late(3)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('delivery_list')), '3 days overdue')

    def test_alerts_center_lists_overdue_deliveries_within_branch_scope(self):
        mine = self._late(2)
        foreign = Procurement.objects.create(
            branch=self.other_branch, supplier=self.supplier, status=P.INCOMING,
            order_date=TODAY, total_cost=0,
            delivery_date=timezone.localdate() - dt.timedelta(days=4))
        self.client.force_login(self.manager)
        page = self.client.get(reverse('alerts_center'))
        self.assertContains(page, 'Overdue deliveries')
        self.assertContains(page, mine.po_number)
        self.assertNotContains(page, foreign.po_number)
        self.assertEqual([p.pk for p in page.context['overdue_deliveries']], [mine.pk])

    def test_overdue_orders_helper_scopes_by_branch(self):
        mine = self._late(2)
        self.assertEqual(list(overdue_orders([self.branch.pk])), [mine])
        self.assertEqual(list(overdue_orders([self.other_branch.pk])), [])
        self.assertEqual(list(overdue_orders(None)), [mine])
