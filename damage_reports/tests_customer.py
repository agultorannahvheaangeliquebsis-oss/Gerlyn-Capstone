"""Customers reporting a problem with a received order: the 7-day window (the
refund window), required photos, per-line quantities, and the hand-off to the
existing refund workflow.

    python manage.py test damage_reports.tests_customer
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Notification
from billing.models import BillingRecord
from damage_reports import services
from damage_reports.errors import DamageReportError, DamageReportForbidden
from damage_reports.models import DamageReport
from damage_reports.testing import (
    FLOOR, DamageFixtures, jpg, png, upload,
)
from inventory.models import Inventory, StockMovement, StockTransfer
from sales.models import Refund, RefundItem, SalesTransaction

Status = DamageReport.Status
Resolution = DamageReport.Resolution


class SevenDayWindowTests(DamageFixtures, TestCase):
    """A customer can report within the same window refunds use, and not after."""

    def setUp(self):
        self.make_world()

    def test_the_window_is_the_refund_window(self):
        self.assertEqual(SalesTransaction.REFUND_WINDOW, timedelta(days=7))
        txn, _ = self.make_order(completed_days_ago=3)
        self.assertEqual(
            services.report_window_closes(txn), txn.completed_at + SalesTransaction.REFUND_WINDOW)

    def test_a_report_inside_the_window_is_accepted(self):
        for days in (0, 1, 6, 6.9):
            with self.subTest(completed_days_ago=days):
                txn, item = self.make_order(completed_days_ago=days)
                report = self.file_customer_report(txn, item)
                self.assertEqual(report.status, Status.PENDING)

    def test_a_report_after_the_window_is_refused(self):
        for days in (7.1, 8, 30):
            with self.subTest(completed_days_ago=days):
                txn, item = self.make_order(completed_days_ago=days)
                with self.assertRaises(DamageReportError) as ctx:
                    self.file_customer_report(txn, item)
                self.assertIn('7-day window', str(ctx.exception))
                self.assertIn('closed', str(ctx.exception))
        self.assertFalse(DamageReport.objects.exists())

    def test_the_last_moment_of_the_window_follows_the_refund_rule_exactly(self):
        txn, item = self.make_order(completed_days_ago=6.999)
        self.assertFalse(txn.refund_window_expired())
        self.file_customer_report(txn, item)

        late, late_item = self.make_order(completed_days_ago=7.001)
        self.assertTrue(late.refund_window_expired())
        with self.assertRaises(DamageReportError):
            self.file_customer_report(late, late_item)

    def test_only_a_received_order_can_be_reported(self):
        for status in (
            SalesTransaction.OrderStatus.PENDING, SalesTransaction.OrderStatus.APPROVED,
            SalesTransaction.OrderStatus.PREPARING, SalesTransaction.OrderStatus.CANCELLED,
        ):
            with self.subTest(status=status):
                txn, item = self.make_order(status=status)
                with self.assertRaises(DamageReportError) as ctx:
                    self.file_customer_report(txn, item)
                self.assertIn('received', str(ctx.exception))
        self.assertFalse(DamageReport.objects.exists())

    def test_an_order_with_no_completion_date_is_not_reportable(self):
        txn, item = self.make_order()
        SalesTransaction.objects.filter(pk=txn.pk).update(completed_at=None)
        with self.assertRaises(DamageReportError):
            self.file_customer_report(txn, item)

    def test_another_customers_order_is_refused(self):
        txn, item = self.make_order(customer=self.other_customer)
        with self.assertRaises(DamageReportForbidden):
            self.file_customer_report(txn, item)
        self.assertFalse(DamageReport.objects.exists())

    def test_staff_cannot_use_the_customer_path(self):
        txn, item = self.make_order()
        for user in (self.staff, self.manager, self.owner):
            with self.subTest(role=user.role):
                with self.assertRaises(DamageReportForbidden):
                    self.file_customer_report(txn, item, customer=user)

    def test_the_form_page_closes_with_the_window(self):
        txn, _ = self.make_order(completed_days_ago=8)
        self.client.force_login(self.customer)
        url = reverse('orders:report_problem', args=[txn.pk])
        response = self.client.get(url, follow=True)
        self.assertRedirects(response, reverse('orders:track_order', args=[txn.pk]))
        self.assertContains(response, 'window to report a problem')


class CustomerReportValidationTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.txn, self.item = self.make_order(quantity=4)

    def refuses(self, **overrides):
        before = DamageReport.objects.count()
        with self.assertRaises(DamageReportError) as ctx:
            self.file_customer_report(self.txn, self.item, **overrides)
        self.assertEqual(DamageReport.objects.count(), before)
        return str(ctx.exception)

    def test_a_valid_report_records_the_order_line_and_branch(self):
        report = self.file_customer_report(self.txn, self.item, quantity='2')

        self.assertEqual(report.reporter_type, 'customer')
        self.assertEqual(report.damage_type, 'customer')
        self.assertEqual(report.reported_by, self.customer)
        self.assertEqual(report.sales_transaction, self.txn)
        self.assertEqual(report.sales_item, self.item)
        self.assertEqual(report.product, self.product)
        self.assertEqual(report.branch, self.branch)
        self.assertEqual(report.quantity, 2)
        self.assertEqual(report.stock_location, '')
        self.assertEqual(report.photos.count(), 1)

    def test_filing_never_touches_stock(self):
        self.file_customer_report(self.txn, self.item, quantity='2')
        self.assertEqual(self.on_hand(FLOOR), 30)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(Refund.objects.exists())

    def test_at_least_one_photo_is_required(self):
        self.assertIn('at least one photo', self.refuses(photos=[]))

    def test_photos_follow_the_same_type_size_and_count_rules(self):
        self.assertIn('JPG or PNG', self.refuses(
            photos=[upload('a.gif', __import__('damage_reports.testing', fromlist=['x'])
                           .image_bytes('GIF'), 'image/gif')]))
        self.assertIn('not a valid image', self.refuses(photos=[upload('a.png', b'hello')]))
        self.assertIn('at most 5', self.refuses(photos=[png(f'{i}.png') for i in range(6)]))

    def test_up_to_five_photos_are_accepted(self):
        report = self.file_customer_report(
            self.txn, self.item, photos=[png(f'{i}.png') for i in range(4)] + [jpg()])
        self.assertEqual(report.photos.count(), 5)

    def test_quantity_cannot_exceed_what_was_bought(self):
        self.assertIn('at most 4', self.refuses(quantity='5'))
        for bad in ('0', '-1', '1.5', 'x', ''):
            with self.subTest(quantity=bad):
                self.refuses(quantity=bad)

    def test_description_is_required(self):
        for text in ('', '   '):
            self.refuses(description=text)

    def test_an_item_from_another_order_cannot_be_named(self):
        _other_txn, other_item = self.make_order(customer=self.other_customer)
        self.refuses(sales_item_id=other_item.pk)
        self.refuses(sales_item_id='abc')
        self.refuses(sales_item_id='')

    def test_open_reports_hold_their_units(self):
        self.file_customer_report(self.txn, self.item, quantity='3')
        self.assertIn('at most 1', self.refuses(quantity='2'))
        self.file_customer_report(self.txn, self.item, quantity='1')
        self.assertIn('already been refunded or reported', self.refuses(quantity='1'))

    def test_a_rejected_report_gives_its_units_back(self):
        report = self.file_customer_report(self.txn, self.item, quantity='4')
        services.reject_report(report.pk, self.manager, reason='Photo shows no damage.')
        self.assertEqual(
            self.file_customer_report(self.txn, self.item, quantity='4').quantity, 4)

    def test_units_already_refunded_cannot_be_reported(self):
        from sales import refunds
        refunds.request_refund(
            self.txn.pk, self.manager, reason_code='Defective', reason='',
            refund_method='Cash',
            lines=[{'item_id': self.item.pk, 'quantity': 3, 'condition': 'Damaged'}])
        self.assertIn('at most 1', self.refuses(quantity='2'))

    def test_reportable_lines_reflect_what_is_left(self):
        self.file_customer_report(self.txn, self.item, quantity='3')
        (row,) = services.reportable_lines(self.txn)
        self.assertEqual((row['item'], row['remaining']), (self.item, 1))


class CustomerNotificationTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.txn, self.item = self.make_order()

    def test_managers_are_told_about_a_new_customer_report(self):
        report = self.file_customer_report(self.txn, self.item)

        recipients = set(Notification.objects.filter(
            event_type='damage_report_submitted').values_list('user__username', flat=True))
        self.assertEqual(recipients, {'dr-owner', 'dr-manager'})
        note = Notification.objects.get(user=self.manager, event_type='damage_report_submitted')
        self.assertIn('A customer reported', note.message)
        self.assertIn(report.report_number, note.title)

    def test_the_customer_is_told_when_the_status_changes_and_linked_to_their_order(self):
        report = self.file_customer_report(self.txn, self.item)
        services.start_review(report.pk, self.manager)
        services.reject_report(report.pk, self.manager, reason='Photo shows no damage.')

        notes = list(Notification.objects.filter(
            user=self.customer, event_type='damage_report_status').order_by('pk'))
        self.assertEqual(len(notes), 2)
        self.assertIn('under review', notes[0].title)
        self.assertIn('rejected', notes[1].title)
        self.assertIn('Photo shows no damage.', notes[1].message)
        for note in notes:
            self.assertEqual(note.action_url, reverse('orders:track_order', args=[self.txn.pk]))
            self.assertEqual(note.category, '')          # a customer notice, not the staff feed
            self.assertIsNone(note.branch)


class CustomerRefundHandoffTests(DamageFixtures, TestCase):
    """Approving a customer report with the refund resolution goes through the
    existing refund workflow - its rules, its bookkeeping, its 7-day window."""

    def setUp(self):
        self.make_world()
        self.txn, self.item = self.make_order(quantity=4, completed_days_ago=2)
        self.report = self.file_customer_report(self.txn, self.item, quantity='1')

    def approve(self, **kw):
        args = dict(resolution=Resolution.REFUND.value, refund_method='Cash')
        args.update(kw)
        return services.approve_report(self.report.pk, self.manager, **args)

    def test_refund_is_raised_and_approved_through_the_refund_workflow(self):
        self.approve(note='Refunded at the till.')

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.APPROVED)
        self.assertEqual(self.report.resolution, Resolution.REFUND)
        refund = self.report.refund
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertEqual(refund.transaction, self.txn)
        self.assertEqual(refund.total_amount, Decimal('20.00'))
        self.assertEqual(refund.refund_type, Refund.RefundType.PARTIAL)
        self.assertEqual(refund.refund_method, 'Cash')
        self.assertEqual(refund.reason_code, Refund.ReasonCode.DEFECTIVE)
        self.assertIn(self.report.report_number, refund.reason)
        self.assertEqual(refund.requested_by, self.manager)
        self.assertEqual(refund.refunded_by, self.manager)
        line = RefundItem.objects.get(refund=refund)
        self.assertEqual((line.sales_item, line.quantity, line.condition, line.amount),
                         (self.item, 1, 'Damaged', Decimal('20.00')))
        self.assertEqual(refund.damage_report, self.report)

    def test_the_orders_money_bookkeeping_is_the_refund_workflows(self):
        self.approve()
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.refunded_amount, Decimal('20.00'))
        self.assertEqual(
            BillingRecord.objects.get(transaction=self.txn).status,
            BillingRecord.PaymentStatus.PARTIALLY_REFUNDED)

    def test_stock_is_not_touched_because_the_goods_were_already_sold(self):
        self.approve()
        self.assertEqual(self.on_hand(FLOOR), 30)
        self.assertEqual(self.batch_qty(self.floor_batch), 30)
        self.assertFalse(StockMovement.objects.exists())
        self.report.refresh_from_db()
        self.assertIsNone(self.report.withdrawal_slip)
        self.assertIsNone(self.report.stock_deducted_at)

    def test_a_refund_method_is_required(self):
        for method in ('', 'Bitcoin'):
            with self.assertRaises(DamageReportError):
                self.approve(refund_method=method)
        self.assertFalse(Refund.objects.exists())

    def test_approving_twice_refunds_once(self):
        self.approve()
        with self.assertRaises(DamageReportError):
            self.approve()
        self.assertEqual(Refund.objects.count(), 1)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.refunded_amount, Decimal('20.00'))

    def test_a_report_filed_in_time_is_still_refunded_when_decided_after_the_window(self):
        # Filed on day 6, decided on day 9.
        SalesTransaction.objects.filter(pk=self.txn.pk).update(
            completed_at=timezone.now() - timedelta(days=9))
        DamageReport.objects.filter(pk=self.report.pk).update(
            created_at=timezone.now() - timedelta(days=3))
        self.txn.refresh_from_db()
        self.assertTrue(self.txn.refund_window_expired())          # closed today...

        self.approve()                                              # ...but filed in time

        self.report.refresh_from_db()
        self.assertEqual(self.report.refund.status, Refund.Status.APPROVED)

    def test_a_report_filed_outside_the_window_cannot_be_refunded(self):
        """Belt and braces: even if a report somehow predates the rule, the
        refund workflow's own window check still refuses it."""
        SalesTransaction.objects.filter(pk=self.txn.pk).update(
            completed_at=timezone.now() - timedelta(days=12))
        DamageReport.objects.filter(pk=self.report.pk).update(
            created_at=timezone.now() - timedelta(days=1))
        with self.assertRaises(DamageReportError) as ctx:
            self.approve()
        self.assertIn('within 7 days', str(ctx.exception))
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.PENDING)
        self.assertFalse(Refund.objects.exists())

    def test_an_order_that_can_no_longer_be_refunded_leaves_the_report_open(self):
        BillingRecord.objects.filter(transaction=self.txn).update(
            status=BillingRecord.PaymentStatus.PENDING)
        with self.assertRaises(DamageReportError):
            self.approve()
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.PENDING)
        self.assertIsNone(self.report.refund)
        self.assertFalse(Refund.objects.exists())

    def test_the_manager_may_resolve_it_without_money_moving(self):
        self.approve(resolution=Resolution.NONE.value)
        self.assertFalse(Refund.objects.exists())
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.refunded_amount, Decimal('0.00'))

    def test_a_refunded_report_no_longer_holds_units_twice(self):
        """The refund holds the unit now; the report must not count it again."""
        self.approve()
        (row,) = services.reportable_lines(self.txn)
        self.assertEqual(row['remaining'], 3)     # 4 bought - 1 refunded, not - 2

    def test_a_staff_report_cannot_be_refunded(self):
        staff_report = self.file_report()
        with self.assertRaises(DamageReportError):
            services.approve_report(
                staff_report.pk, self.manager, resolution=Resolution.REFUND.value,
                refund_method='Cash')


class CustomerReplacementTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.txn, self.item = self.make_order(quantity=4)
        self.report = self.file_customer_report(self.txn, self.item, quantity='2')

    def test_a_replacement_comes_off_the_floor_with_a_slip_and_no_refund(self):
        services.approve_report(
            self.report.pk, self.manager, resolution=Resolution.REPLACEMENT.value)

        self.report.refresh_from_db()
        self.assertEqual(self.on_hand(FLOOR), 28)
        self.assertEqual(StockMovement.objects.get().quantity_change, -2)
        slip = self.report.withdrawal_slip
        self.assertEqual(slip.purpose, StockTransfer.Purpose.OTHER)
        self.assertEqual(slip.requested_by, self.manager)       # never the customer
        self.assertIn('customer replacement', slip.notes)
        self.assertFalse(Refund.objects.exists())

    def test_not_enough_floor_stock_to_replace_refuses_cleanly(self):
        Inventory.objects.filter(branch=self.branch, product=self.product).update(
            quantity_on_hand=1)
        with self.assertRaises(DamageReportError):
            services.approve_report(
                self.report.pk, self.manager, resolution=Resolution.REPLACEMENT.value)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Status.PENDING)
        self.assertFalse(StockTransfer.objects.exists())

    def test_replacing_twice_deducts_once(self):
        services.approve_report(
            self.report.pk, self.manager, resolution=Resolution.REPLACEMENT.value)
        with self.assertRaises(DamageReportError):
            services.approve_report(
                self.report.pk, self.manager, resolution=Resolution.REPLACEMENT.value)
        self.assertEqual(self.on_hand(FLOOR), 28)


class CustomerViewTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.txn, self.item = self.make_order(quantity=4)
        self.url = reverse('orders:report_problem', args=[self.txn.pk])
        self.track = reverse('orders:track_order', args=[self.txn.pk])
        self.client.force_login(self.customer)

    def post(self, **overrides):
        data = {
            'sales_item_id': self.item.pk, 'quantity': '1',
            'description': 'The can was dented and leaking.', 'photos': [png()],
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_the_form_lists_the_items_and_requires_photos(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Canned Sardines 155g')
        self.assertContains(response, 'you can report up to 4')
        self.assertContains(response, 'accept="image/jpeg,image/png"')
        self.assertContains(response, 'required accept')       # the photo input

    def test_submitting_files_the_report_and_returns_to_the_order(self):
        response = self.post()

        self.assertRedirects(response, self.track)
        report = DamageReport.objects.get()
        self.assertEqual(report.reported_by, self.customer)
        self.assertEqual(report.photos.count(), 1)

    def test_a_missing_photo_redraws_the_form_with_the_message(self):
        response = self.post(photos=[])
        self.assertContains(response, 'at least one photo')
        self.assertContains(response, 'dented and leaking')       # input kept
        self.assertFalse(DamageReport.objects.exists())

    def test_a_non_image_is_refused(self):
        response = self.post(photos=[upload('virus.png', b'MZ\x90\x00')])
        self.assertContains(response, 'not a valid image')
        self.assertFalse(DamageReport.objects.exists())

    def test_the_form_is_closed_outside_the_window(self):
        SalesTransaction.objects.filter(pk=self.txn.pk).update(
            completed_at=timezone.now() - timedelta(days=8))
        self.assertRedirects(self.client.get(self.url), self.track)
        self.assertRedirects(self.post(), self.track)
        self.assertFalse(DamageReport.objects.exists())

    def test_someone_elses_order_is_a_404(self):
        self.client.force_login(self.other_customer)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.post().status_code, 404)
        self.assertFalse(DamageReport.objects.exists())

    def test_staff_get_a_403_and_visitors_are_sent_to_sign_in(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.post().status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertFalse(DamageReport.objects.exists())

    def test_a_posted_branch_or_status_is_ignored(self):
        self.post(branch_id=self.other_branch.pk, status='Approved', resolution='refund')
        report = DamageReport.objects.get()
        self.assertEqual(report.branch, self.branch)
        self.assertEqual(report.status, Status.PENDING)
        self.assertEqual(report.resolution, Resolution.NONE)

    def test_track_order_offers_the_button_only_inside_the_window(self):
        self.assertContains(self.client.get(self.track), self.url)
        SalesTransaction.objects.filter(pk=self.txn.pk).update(
            completed_at=timezone.now() - timedelta(days=8))
        self.assertNotContains(self.client.get(self.track), self.url)

    def test_track_order_does_not_offer_it_before_the_order_is_received(self):
        txn, _ = self.make_order(status=SalesTransaction.OrderStatus.PREPARING)
        page = self.client.get(reverse('orders:track_order', args=[txn.pk]))
        self.assertNotContains(page, reverse('orders:report_problem', args=[txn.pk]))

    def test_track_order_lists_the_report_in_plain_words_with_its_photo(self):
        self.post()
        report = DamageReport.objects.get()
        photo = report.photos.get()

        page = self.client.get(self.track)

        self.assertContains(page, report.report_number)
        self.assertContains(page, 'Received')       # customer wording for Pending
        self.assertContains(page, reverse(
            'orders:problem_photo', args=[self.txn.pk, report.pk, photo.pk]))

    def test_a_rejected_report_shows_the_reason_but_never_internal_notes(self):
        self.post()
        report = DamageReport.objects.get()
        services.reject_report(report.pk, self.manager, reason='Photo shows no damage.')
        page = self.client.get(self.track)
        self.assertContains(page, 'Photo shows no damage.')
        self.assertContains(page, 'Not approved')

        other_txn, other_item = self.make_order()
        approved = self.file_customer_report(other_txn, other_item)
        services.approve_report(
            approved.pk, self.manager, resolution=Resolution.NONE.value,
            note='INTERNAL: suspect repeat claimant')
        page = self.client.get(reverse('orders:track_order', args=[other_txn.pk]))
        self.assertNotContains(page, 'INTERNAL')

    def test_the_customer_sees_a_refund_outcome(self):
        self.post()
        report = DamageReport.objects.get()
        services.approve_report(
            report.pk, self.manager, resolution=Resolution.REFUND.value, refund_method='GCash')
        self.assertContains(self.client.get(self.track), 'refund of ₱20.00')

    def test_a_customer_report_is_visible_to_the_branch_manager_with_the_order(self):
        self.post()
        report = DamageReport.objects.get()
        self.client.force_login(self.manager)
        detail = self.client.get(reverse('damage_report_detail', args=[report.pk]))
        self.assertContains(detail, self.txn.order_number)
        self.assertContains(detail, 'Customer-reported')
        self.assertContains(detail, 'Refund')


class CustomerPhotoPrivacyTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.txn, self.item = self.make_order()
        self.report = self.file_customer_report(self.txn, self.item)
        self.photo = self.report.photos.get()
        self.url = reverse(
            'orders:problem_photo', args=[self.txn.pk, self.report.pk, self.photo.pk])

    def test_the_owner_of_the_order_gets_the_image(self):
        self.client.force_login(self.customer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('private', response['Cache-Control'])
        self.assertTrue(b''.join(response.streaming_content).startswith(b'\x89PNG'))

    def test_other_customers_and_wrong_ids_get_a_404(self):
        self.client.force_login(self.other_customer)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.customer)
        other_txn, _ = self.make_order()
        wrong = reverse('orders:problem_photo', args=[other_txn.pk, self.report.pk, self.photo.pk])
        self.assertEqual(self.client.get(wrong).status_code, 404)

    def test_visitors_are_sent_to_sign_in(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_a_customer_cannot_use_the_staff_photo_route(self):
        self.client.force_login(self.customer)
        staff_url = reverse('damage_report_photo', args=[self.report.pk, self.photo.pk])
        self.assertEqual(self.client.get(staff_url).status_code, 403)

    def test_a_staff_report_photo_is_not_reachable_through_the_customer_route(self):
        staff_report = self.file_report()
        photo = staff_report.photos.get()
        self.client.force_login(self.customer)
        url = reverse('orders:problem_photo', args=[self.txn.pk, staff_report.pk, photo.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_file_that_is_not_a_png_or_jpeg_is_never_served(self):
        self.photo.image.name = self.photo.image.name.rsplit('.', 1)[0] + '.html'
        self.photo.save()
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_photo_is_not_under_the_public_media_url(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get('/media/' + self.photo.image.name).status_code, 404)


class CustomerFilingInAutocommitTests(DamageFixtures, TransactionTestCase):
    """The customer path locks the order row; run it with no wrapping
    transaction, as production does."""

    def setUp(self):
        self.make_world()
        self.txn, self.item = self.make_order()

    def test_a_customer_can_file_through_the_real_view(self):
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse('orders:report_problem', args=[self.txn.pk]),
            {'sales_item_id': self.item.pk, 'quantity': '1',
             'description': 'Leaking.', 'photos': [png()]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DamageReport.objects.get().sales_transaction, self.txn)

    def test_a_manager_can_refund_through_the_real_view(self):
        report = self.file_customer_report(self.txn, self.item)
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('damage_report_approve', args=[report.pk]),
            {'resolution': 'refund', 'refund_method': 'Cash'})
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.refund.status, Refund.Status.APPROVED)
