"""Stock Count (cycle count) sessions — inventory/stock_count.py.

Covers the snapshot, one-open-session rule, branch/role/module gates, scan
resolution and logging, the uncounted policy, maker/checker, the drift rule,
exactly-once approval, negative-stock refusal and input limits.

Services called directly here set the audit action label on a module-level
ContextVar outside any request; every fixture registers a cleanup that resets
it, or the next test's audit rows would carry the label (see
inventory/tests_bulk_floor_refill.py).

The approve path is also exercised in a TransactionTestCase: a plain
TestCase wraps each test in a transaction, which hides a select_for_update()
issued outside transaction.atomic() — exactly the bug class this codebase has
shipped before (there is no ATOMIC_REQUESTS).
"""
from datetime import date
from unittest import mock

from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import Notification, User
from audit.context import set_action_label
from audit.models import AuditLog
from branches.models import Branch
from core.limits import MAX_QUANTITY
from core.module_permissions import (
    ADD, DELETE, EDIT, FLOOR_STOCK, STOCK_COUNT, URL_NAME_TO_MODULE, VIEW,
    WAREHOUSE_STOCK, required_action_for_request, role_permission_defaults,
)
from core.notifications import EVENT_STOCK_COUNT_SUBMITTED
from inventory.models import (
    Batch, Inventory, Product, ProductCategory, StockCount, StockCountLine,
    StockCountScanLog, StockMovement, WarehouseStock,
)
from inventory.stock_count import (
    StockCountAlreadyApproved, StockCountError, StockCountStaleError,
    approve_stock_count, cancel_stock_count, recount_stock_count,
    reject_stock_count, save_counts, start_stock_count, submit_stock_count,
)

Status = StockCount.Status


class _Fixture:
    """Branch A floor: P1 10, P2 5, P3 7 (P3 in another category).
    Branch A warehouse: P1 40. Branch B floor: P1 3."""

    def build(self):
        self.addCleanup(set_action_label, '')
        self.owner = User.objects.create_user(username='sc-owner', password='pw', role=User.Role.OWNER)
        self.a = Branch.objects.create(owner=self.owner, branch_name='Branch A', location='A')
        self.b = Branch.objects.create(owner=self.owner, branch_name='Branch B', location='B')
        self.grocery = ProductCategory.objects.create(category_name='Grocery')
        self.cleaning = ProductCategory.objects.create(category_name='Cleaning')
        self.p1 = Product.objects.create(
            sku_code='SC-P1', barcode='4800001', product_name='Rice 5kg',
            unit_price='250.00', category=self.grocery,
        )
        self.p2 = Product.objects.create(
            sku_code='SC-P2', product_name='Loose Eggs', unit_price='9.00',
            category=self.grocery,
        )
        self.p3 = Product.objects.create(
            sku_code='SC-P3', barcode='4800003', product_name='Bleach 1L',
            unit_price='60.00', category=self.cleaning,
        )
        self.p4 = Product.objects.create(
            sku_code='SC-P4', barcode='4800004', product_name='Soap Bar', unit_price='30.00',
        )
        self._floor(self.a, self.p1, 10)
        self._floor(self.a, self.p2, 5)
        self._floor(self.a, self.p3, 7)
        self._floor(self.b, self.p1, 3)
        self._warehouse(self.a, self.p1, 40)

        self.manager = self._internal('sc-mgr', User.Role.BRANCH_MANAGER, self.a)
        self.manager2 = self._internal('sc-mgr2', User.Role.BRANCH_MANAGER, self.a)
        self.staff = self._internal('sc-staff', User.Role.STAFF, self.a)
        self.staff2 = self._internal('sc-staff2', User.Role.STAFF, self.a)
        self.manager_b = self._internal('sc-mgr-b', User.Role.BRANCH_MANAGER, self.b)
        self.supplier = User.objects.create_user(username='sc-sup', password='pw', role=User.Role.SUPPLIER)
        self.customer = User.objects.create_user(username='sc-cust', password='pw', role=User.Role.CUSTOMER)

    def _internal(self, username, role, branch):
        user = User.objects.create_user(username=username, password='pw', role=role)
        profile = user.manager_profile if role == User.Role.BRANCH_MANAGER else user.staff_profile
        profile.branch = branch
        profile.save(update_fields=['branch'])
        return User.objects.get(pk=user.pk)

    def _floor(self, branch, product, qty):
        Inventory.objects.create(branch=branch, product=product, quantity_on_hand=qty)
        Batch.objects.create(
            product=product, branch=branch, location=Batch.Location.FLOOR,
            quantity=qty, received_date=date.today(),
        )

    def _warehouse(self, branch, product, qty):
        WarehouseStock.objects.create(warehouse=branch.warehouse, product=product, quantity_on_hand=qty)
        Batch.objects.create(
            product=product, branch=branch, location=Batch.Location.WAREHOUSE,
            quantity=qty, received_date=date.today(),
        )

    def _sell(self, product, qty, branch=None):
        """What a floor sale does to stock: aggregate, lots and ledger."""
        branch = branch or self.a
        with transaction.atomic():
            inv = Inventory.objects.select_for_update().get(branch=branch, product=product)
            before = inv.quantity_on_hand
            Batch.consume_fefo(product, branch, Batch.Location.FLOOR, qty)
            inv.quantity_on_hand -= qty
            inv.save()
            StockMovement.objects.create(
                inventory=inv, location_type=StockMovement.LocationType.FLOOR,
                movement_type=StockMovement.MovementType.SALE, quantity_change=-qty,
                quantity_before=before, quantity_after=inv.quantity_on_hand,
            )

    def floor_qty(self, product, branch=None):
        return Inventory.objects.get(branch=branch or self.a, product=product).quantity_on_hand

    def floor_batch_total(self, product, branch=None):
        return sum(Batch.objects.filter(
            branch=branch or self.a, product=product, location=Batch.Location.FLOOR,
        ).values_list('quantity', flat=True))

    def start(self, actor=None, location=Batch.Location.FLOOR, category=None, branch=None):
        return start_stock_count(
            branch=branch or self.a, location_type=location,
            actor=actor or self.staff, category=category,
        )

    def line(self, count, product):
        return count.lines.get(product=product)

    def count_values(self, count, actor, values):
        """values: {product: qty or None} -> save_counts as a form would."""
        changes = {}
        for product, qty in values.items():
            line = self.line(count, product)
            changes[line.pk] = (line.counted_qty, qty)
        return save_counts(count_id=count.pk, actor=actor, changes=changes)

    def submit(self, count, actor, policy=StockCount.UncountedPolicy.LEAVE):
        return submit_stock_count(count_id=count.pk, actor=actor, uncounted_policy=policy)

    def approve(self, count, actor, accept=None, reasons=None, default_reason='Cycle count'):
        count.refresh_from_db()
        if accept is None:
            accept = set(count.lines.values_list('pk', flat=True))
        return approve_stock_count(
            count_id=count.pk, actor=actor, expected_status=count.status,
            accepted=set(accept), reasons=reasons or {}, default_reason=default_reason,
        )


class StartSnapshotTests(_Fixture, TestCase):
    def setUp(self):
        self.build()

    def test_start_snapshots_every_floor_row_of_the_branch(self):
        count = self.start()
        self.assertEqual(count.status, Status.DRAFT)
        self.assertRegex(count.count_number, r'^SC-\d{4}-\d{3,}$')
        snapshot = {line.product_id: line.expected_qty for line in count.lines.all()}
        self.assertEqual(snapshot, {self.p1.pk: 10, self.p2.pk: 5, self.p3.pk: 7})
        self.assertTrue(all(line.counted_qty is None for line in count.lines.all()))
        self.assertEqual(count.events.get().to_status, Status.DRAFT)

    def test_category_filter_and_warehouse_location(self):
        floor = self.start(category=self.cleaning)
        self.assertEqual(list(floor.lines.values_list('product_id', flat=True)), [self.p3.pk])
        warehouse = self.start(location=Batch.Location.WAREHOUSE)
        self.assertEqual(
            {line.product_id: line.expected_qty for line in warehouse.lines.all()},
            {self.p1.pk: 40},
        )

    def test_variance_is_measured_against_the_snapshot_not_the_live_row(self):
        count = self.start()
        Inventory.objects.filter(branch=self.a, product=self.p1).update(quantity_on_hand=99)
        self.assertEqual(self.line(count, self.p1).expected_qty, 10)

    def test_nothing_to_count_is_refused(self):
        with self.assertRaises(StockCountError):
            self.start(location=Batch.Location.WAREHOUSE, branch=self.b, actor=self.manager_b)

    def test_line_cap(self):
        with mock.patch('inventory.stock_count.MAX_STOCK_COUNT_LINES', 2):
            with self.assertRaisesMessage(StockCountError, 'at most 2'):
                self.start()
        self.assertFalse(StockCount.objects.exists())

    def test_start_via_the_form(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('stock_count_start'), {
            'location': 'Floor', 'category': str(self.grocery.pk), 'notes': 'Aisle 1',
            # A Staff account cannot pick another branch, whatever it posts.
            'branch_id': str(self.b.pk),
        })
        count = StockCount.objects.get()
        self.assertRedirects(response, reverse('stock_count_count', args=[count.pk]))
        self.assertEqual(count.branch, self.a)
        self.assertEqual(count.created_by, self.staff)
        self.assertEqual(count.lines.count(), 2)

    def test_owner_picks_the_branch(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('stock_count_start'), {'location': 'Floor', 'branch_id': str(self.b.pk)})
        self.assertEqual(StockCount.objects.get().branch, self.b)

    def test_unassigned_staff_cannot_start(self):
        self.staff.staff_profile.branch = None
        self.staff.staff_profile.save()
        self.client.force_login(self.staff)
        response = self.client.post(reverse('stock_count_start'), {'location': 'Floor', 'branch_id': str(self.a.pk)})
        self.assertRedirects(response, reverse('stock_count_list'))
        self.assertFalse(StockCount.objects.exists())


class OneOpenSessionTests(_Fixture, TestCase):
    def setUp(self):
        self.build()

    def test_second_open_session_for_the_same_branch_and_location_is_refused(self):
        first = self.start()
        with self.assertRaisesMessage(StockCountError, first.count_number):
            self.start(actor=self.manager)
        self.assertEqual(StockCount.objects.count(), 1)

    def test_other_location_and_other_branch_are_independent(self):
        self.start()
        self.start(location=Batch.Location.WAREHOUSE)
        self.start(branch=self.b, actor=self.manager_b)
        self.assertEqual(StockCount.objects.count(), 3)

    def test_a_closed_session_frees_the_slot(self):
        first = self.start()
        cancel_stock_count(count_id=first.pk, actor=self.staff)
        first.refresh_from_db()
        self.assertIsNone(first.open_slot)
        self.start()
        self.assertEqual(StockCount.objects.filter(open_slot__isnull=False).count(), 1)

    def test_the_form_shows_a_clear_message(self):
        self.start()
        self.client.force_login(self.manager)
        response = self.client.post(reverse('stock_count_start'), {'location': 'Floor'}, follow=True)
        self.assertContains(response, 'is still open')
        self.assertEqual(StockCount.objects.count(), 1)


class AccessTests(_Fixture, TestCase):
    def setUp(self):
        self.build()
        self.count = self.start()

    def _urls(self):
        pk = self.count.pk
        return [
            ('get', reverse('stock_count_list')),
            ('get', reverse('stock_count_start')),
            ('get', reverse('stock_count_detail', args=[pk])),
            ('get', reverse('stock_count_count', args=[pk])),
            ('post', reverse('stock_count_scan', args=[pk])),
            ('get', reverse('stock_count_review', args=[pk])),
            ('post', reverse('stock_count_reject', args=[pk])),
            ('post', reverse('stock_count_recount', args=[pk])),
            ('post', reverse('stock_count_cancel', args=[pk])),
        ]

    def test_supplier_and_customer_get_a_real_403(self):
        for user in (self.supplier, self.customer):
            self.client.force_login(user)
            for method, url in self._urls():
                response = getattr(self.client, method)(url, {'code': '4800001'})
                self.assertEqual(response.status_code, 403, (user.username, url))
        self.assertEqual(self.count.scan_logs.count(), 0)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse('stock_count_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_manager_of_another_branch_cannot_see_or_act(self):
        self.client.force_login(self.manager_b)
        self.assertNotContains(self.client.get(reverse('stock_count_list')), self.count.count_number)
        for method, url in self._urls()[2:]:
            response = getattr(self.client, method)(url, {'code': '4800001', 'reason': 'x'})
            self.assertEqual(response.status_code, 404, url)
        self.count.refresh_from_db()
        self.assertEqual(self.count.status, Status.DRAFT)

    def test_owner_sees_every_branch(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('stock_count_list')), self.count.count_number)
        self.assertEqual(self.client.get(reverse('stock_count_detail', args=[self.count.pk])).status_code, 200)

    def test_staff_can_count_but_never_review(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('stock_count_count', args=[self.count.pk])).status_code, 200)
        # Module default: Staff has no Edit on Stock Counts -> styled 403.
        self.assertEqual(self.client.get(reverse('stock_count_review', args=[self.count.pk])).status_code, 403)
        # Even with Edit granted, the role check refuses the approval itself.
        actions = dict(self.staff2.module_actions)
        actions[STOCK_COUNT] = [ADD, EDIT, DELETE]
        self.staff2.module_actions = actions
        self.staff2.save(update_fields=['module_actions'])
        self.count_values(self.count, self.staff, {self.p1: 9})
        self.submit(self.count, self.staff)
        with self.assertRaisesMessage(StockCountError, 'Only the Owner or a Branch Manager'):
            self.approve(self.count, self.staff2)

    def test_revoking_the_module_closes_the_direct_urls(self):
        self.staff.allowed_modules = [m for m in self.staff.allowed_modules if m != STOCK_COUNT]
        self.staff.save(update_fields=['allowed_modules'])
        self.client.force_login(self.staff)
        for method, url in self._urls():
            response = getattr(self.client, method)(url, {'code': '4800001'})
            self.assertEqual(response.status_code, 403, url)

    def test_revoking_add_keeps_view_only(self):
        actions = dict(self.staff.module_actions)
        actions[STOCK_COUNT] = []
        self.staff.module_actions = actions
        self.staff.save(update_fields=['module_actions'])
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('stock_count_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('stock_count_count', args=[self.count.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('stock_count_scan', args=[self.count.pk]), {'code': 'x'}).status_code, 403)

    def test_counting_a_location_needs_that_stock_module(self):
        self.staff.allowed_modules = [m for m in self.staff.allowed_modules if m != FLOOR_STOCK]
        self.staff.save(update_fields=['allowed_modules'])
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('stock_count_detail', args=[self.count.pk])).status_code, 404)
        self.client.post(reverse('stock_count_start'), {'location': 'Floor'})
        self.assertEqual(StockCount.objects.count(), 1)
        # Warehouse is still theirs to count.
        self.client.post(reverse('stock_count_start'), {'location': 'Warehouse'})
        self.assertEqual(StockCount.objects.count(), 2)


class ScanTests(_Fixture, TestCase):
    def setUp(self):
        self.build()
        self.count = self.start()
        self.client.force_login(self.staff)
        self.url = reverse('stock_count_scan', args=[self.count.pk])

    def test_barcode_matches_case_insensitively_then_sku(self):
        self.p3.barcode = 'abc-777'
        self.p3.save()
        data = self.client.post(self.url, {'code': 'ABC-777'}).json()
        self.assertEqual((data['status'], data['line_id']), ('matched', self.line(self.count, self.p3).pk))
        data = self.client.post(self.url, {'code': 'sc-p2'}).json()
        self.assertEqual((data['status'], data['line_id']), ('matched', self.line(self.count, self.p2).pk))

    def test_unknown_and_not_in_count_codes_are_logged_as_unexpected(self):
        self.assertEqual(self.client.post(self.url, {'code': 'NOPE-1'}).json()['status'], 'unknown')
        data = self.client.post(self.url, {'code': '4800004'}).json()
        self.assertEqual((data['status'], data['product_id']), ('not_in_count', self.p4.pk))
        logs = list(StockCountScanLog.objects.order_by('pk'))
        self.assertEqual([(l.scanned_code, l.was_unexpected, l.line_id) for l in logs],
                         [('NOPE-1', True, None), ('4800004', True, None)])
        self.assertEqual(logs[1].matched_product, self.p4)

    def test_scanning_logs_every_read_and_never_changes_a_count(self):
        for _ in range(3):
            self.client.post(self.url, {'code': '4800001'})
        line = self.line(self.count, self.p1)
        self.assertIsNone(line.counted_qty)
        self.assertEqual(line.scan_logs.count(), 3)
        self.assertTrue(all(log.scanned_by == self.staff for log in line.scan_logs.all()))
        # The form's running total (3 scans = 3) is what gets saved.
        self.client.post(reverse('stock_count_count', args=[self.count.pk]), {
            f'counted_{line.pk}': '3', f'orig_{line.pk}': '', 'action': 'save',
        })
        self.assertEqual(self.line(self.count, self.p1).counted_qty, 3)

    def test_scan_refuses_closed_sessions_and_oversized_codes(self):
        self.assertEqual(self.client.post(self.url, {'code': 'x' * 65}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'code': ''}).status_code, 400)
        cancel_stock_count(count_id=self.count.pk, actor=self.staff)
        self.assertEqual(self.client.post(self.url, {'code': '4800001'}).status_code, 400)
        self.assertEqual(StockCountScanLog.objects.count(), 0)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url, {'code': '4800001'}).status_code, 405)


class CountingFormTests(_Fixture, TestCase):
    def setUp(self):
        self.build()
        self.count = self.start()
        self.client.force_login(self.staff)
        self.url = reverse('stock_count_count', args=[self.count.pk])

    def test_only_lines_changed_in_this_form_are_written(self):
        l1, l2 = self.line(self.count, self.p1), self.line(self.count, self.p2)
        # Another phone already saved P2 = 4.
        self.count_values(self.count, self.staff2, {self.p2: 4})
        # This phone's form was rendered before that: P2 still blank here.
        self.client.post(self.url, {
            f'counted_{l1.pk}': '9', f'orig_{l1.pk}': '',
            f'counted_{l2.pk}': '', f'orig_{l2.pk}': '',
            'action': 'save',
        })
        l1.refresh_from_db()
        l2.refresh_from_db()
        self.assertEqual((l1.counted_qty, l1.counted_by), (9, self.staff))
        self.assertEqual((l2.counted_qty, l2.counted_by), (4, self.staff2))
        self.count.refresh_from_db()
        self.assertEqual(self.count.status, Status.COUNTING)

    def test_input_limits(self):
        line = self.line(self.count, self.p1)
        for bad in (str(MAX_QUANTITY + 1), '-1', '2.5', 'abc', '9' * 40):
            response = self.client.post(self.url, {f'counted_{line.pk}': bad, f'orig_{line.pk}': '', 'action': 'save'})
            self.assertEqual(response.status_code, 200, bad)
            self.assertIsNone(self.line(self.count, self.p1).counted_qty, bad)
        self.client.post(self.url, {f'counted_{line.pk}': str(MAX_QUANTITY), f'orig_{line.pk}': '', 'action': 'save'})
        self.assertEqual(self.line(self.count, self.p1).counted_qty, MAX_QUANTITY)

    def test_add_unexpected_item_saves_counts_in_the_same_request(self):
        l1 = self.line(self.count, self.p1)
        self.client.post(self.url, {
            f'counted_{l1.pk}': '8', f'orig_{l1.pk}': '',
            'action': 'add', 'add_product_id': str(self.p4.pk),
        })
        added = self.line(self.count, self.p4)
        self.assertTrue(added.is_unexpected)
        self.assertEqual(added.expected_qty, 0)
        self.assertEqual(self.line(self.count, self.p1).counted_qty, 8)
        # Adding it again does not duplicate the line.
        self.client.post(self.url, {'action': 'add', 'add_product_id': str(self.p4.pk)})
        self.assertEqual(self.count.lines.filter(product=self.p4).count(), 1)

    def test_lines_from_another_count_are_ignored(self):
        other = self.start(location=Batch.Location.WAREHOUSE)
        foreign = other.lines.get()
        self.client.post(self.url, {f'counted_{foreign.pk}': '1', f'orig_{foreign.pk}': '', 'action': 'save'})
        foreign.refresh_from_db()
        self.assertIsNone(foreign.counted_qty)

    def test_submit_requires_at_least_one_count(self):
        with self.assertRaisesMessage(StockCountError, 'at least one'):
            self.submit(self.count, self.staff)

    def test_submitted_counts_are_frozen(self):
        self.count_values(self.count, self.staff, {self.p1: 9})
        self.submit(self.count, self.staff)
        with self.assertRaises(StockCountStaleError):
            self.count_values(self.count, self.staff, {self.p1: 1})
        self.assertRedirects(self.client.get(self.url), reverse('stock_count_detail', args=[self.count.pk]))

    def test_submit_notifies_owner_and_other_managers_not_staff_or_the_submitter(self):
        line = self.line(self.count, self.p1)
        self.client.post(self.url, {
            f'counted_{line.pk}': '9', f'orig_{line.pk}': '', 'action': 'submit',
            'uncounted_policy': 'leave',
        })
        self.count.refresh_from_db()
        self.assertEqual(self.count.status, Status.SUBMITTED)
        recipients = set(Notification.objects.filter(
            event_type=EVENT_STOCK_COUNT_SUBMITTED,
        ).values_list('user__username', flat=True))
        self.assertEqual(recipients, {'sc-owner', 'sc-mgr', 'sc-mgr2'})

    def test_counting_screen_is_blind_and_unlocalizes_ids(self):
        response = self.client.get(self.url)
        line = self.line(self.count, self.p1)
        self.assertContains(response, f'name="counted_{line.pk}"')
        self.assertNotContains(response, 'At start')


class UncountedPolicyTests(_Fixture, TestCase):
    def setUp(self):
        self.build()
        self.count = self.start()
        self.count_values(self.count, self.staff, {self.p1: 9})

    def test_default_leaves_uncounted_items_untouched(self):
        self.submit(self.count, self.staff)
        self.approve(self.count, self.manager)
        self.assertEqual((self.floor_qty(self.p1), self.floor_qty(self.p2), self.floor_qty(self.p3)), (9, 5, 7))
        self.assertEqual(self.line(self.count, self.p2).decision, StockCountLine.Decision.UNCOUNTED)

    def test_treat_as_zero_zeroes_them(self):
        self.submit(self.count, self.staff, policy=StockCount.UncountedPolicy.ZERO)
        self.approve(self.count, self.manager)
        self.assertEqual((self.floor_qty(self.p1), self.floor_qty(self.p2), self.floor_qty(self.p3)), (9, 0, 0))
        self.assertEqual(self.floor_batch_total(self.p2), 0)

    def test_policy_must_be_a_known_value(self):
        with self.assertRaises(StockCountError):
            self.submit(self.count, self.staff, policy='everything')


class MakerCheckerTests(_Fixture, TestCase):
    def setUp(self):
        self.build()

    def test_a_manager_who_counted_cannot_approve_their_own_count(self):
        count = self.start(actor=self.manager)
        self.count_values(count, self.manager, {self.p1: 9})
        self.submit(count, self.manager)
        with self.assertRaisesMessage(StockCountError, 'someone else must review it'):
            self.approve(count, self.manager)
        self.assertEqual(self.floor_qty(self.p1), 10)
        # A different manager may.
        self.approve(count, self.manager2)
        self.assertEqual(self.floor_qty(self.p1), 9)

    def test_scanning_alone_makes_you_a_maker(self):
        count = self.start(actor=self.staff)
        self.client.force_login(self.manager)
        self.client.post(reverse('stock_count_scan', args=[count.pk]), {'code': '4800001'})
        self.count_values(count, self.staff, {self.p1: 9})
        self.submit(count, self.staff)
        with self.assertRaises(StockCountError):
            self.approve(count, self.manager)

    def test_reject_and_recount_follow_the_same_rule(self):
        count = self.start(actor=self.manager)
        self.count_values(count, self.manager, {self.p1: 9})
        self.submit(count, self.manager)
        with self.assertRaises(StockCountError):
            reject_stock_count(count_id=count.pk, actor=self.manager, expected_status=Status.SUBMITTED, reason='x')
        with self.assertRaises(StockCountError):
            recount_stock_count(count_id=count.pk, actor=self.manager, expected_status=Status.SUBMITTED,
                                line_ids=[self.line(count, self.p1).pk])

    def test_the_owner_may_review_their_own_count(self):
        count = self.start(actor=self.owner)
        self.count_values(count, self.owner, {self.p1: 12})
        self.client.force_login(self.owner)
        line = self.line(count, self.p1)
        response = self.client.post(reverse('stock_count_count', args=[count.pk]), {
            f'counted_{line.pk}': '12', f'orig_{line.pk}': '12', 'action': 'submit',
            'uncounted_policy': 'leave',
        })
        # Straight to the review screen rather than silently applied.
        self.assertRedirects(response, reverse('stock_count_review', args=[count.pk]))
        self.assertEqual(self.floor_qty(self.p1), 10)
        self.approve(count, self.owner)
        self.assertEqual(self.floor_qty(self.p1), 12)

    def test_review_page_explains_why_a_maker_cannot_approve(self):
        count = self.start(actor=self.manager)
        self.count_values(count, self.manager, {self.p1: 9})
        self.submit(count, self.manager)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('stock_count_review', args=[count.pk]))
        self.assertContains(response, 'someone else must review it')
        self.assertNotContains(response, 'Approve and Apply')


class ApproveTests(_Fixture, TestCase):
    def setUp(self):
        self.build()
        self.count = self.start()
        self.count_values(self.count, self.staff, {self.p1: 8, self.p2: 5, self.p3: 9})
        self.submit(self.count, self.staff)

    def test_applies_exactly_the_variance_with_a_movement_and_audit_rows(self):
        self.client.force_login(self.manager)
        lines = {line.product_id: line for line in self.count.lines.all()}
        response = self.client.post(reverse('stock_count_review', args=[self.count.pk]), {
            'expected_status': 'Submitted',
            'accept': [str(lines[self.p1.pk].pk), str(lines[self.p3.pk].pk)],
            f'reason_{lines[self.p1.pk].pk}': 'Damaged sacks',
            'default_reason': 'Found on lower shelf',
        })
        self.assertRedirects(response, reverse('stock_count_detail', args=[self.count.pk]))
        self.assertEqual((self.floor_qty(self.p1), self.floor_qty(self.p2), self.floor_qty(self.p3)), (8, 5, 9))
        self.assertEqual(self.floor_batch_total(self.p1), 8)
        self.assertEqual(self.floor_batch_total(self.p3), 9)

        movements = StockMovement.objects.filter(movement_type=StockMovement.MovementType.STOCK_COUNT)
        self.assertEqual(
            sorted((m.inventory.product_id, m.quantity_change, m.quantity_before, m.quantity_after) for m in movements),
            sorted([(self.p1.pk, -2, 10, 8), (self.p3.pk, 2, 7, 9)]),
        )
        self.assertTrue(all(m.reference_id == self.count.pk and m.created_by == self.manager for m in movements))
        self.assertIn('Damaged sacks', movements.get(inventory__product=self.p1).notes)
        self.assertIn('Found on lower shelf', movements.get(inventory__product=self.p3).notes)

        self.count.refresh_from_db()
        self.assertEqual((self.count.status, self.count.approved_by, self.count.open_slot),
                         (Status.APPROVED, self.manager, None))
        p1_line = self.line(self.count, self.p1)
        self.assertEqual((p1_line.decision, p1_line.applied_change, p1_line.movement.quantity_change),
                         ('applied', -2, -2))
        self.assertEqual(self.line(self.count, self.p2).decision, 'no_change')

        labelled = AuditLog.objects.filter(action_label='Approved a stock count and applied its variances')
        self.assertTrue(labelled.filter(entity='inventory.StockCount', record_id=str(self.count.pk)).exists())
        self.assertTrue(labelled.filter(entity='inventory.StockCountLine').exists())
        self.assertTrue(AuditLog.objects.filter(
            entity='inventory.StockCount', summary__contains='Rice 5kg -2',
        ).exists())

    def test_unticked_variance_is_not_applied(self):
        p3 = self.line(self.count, self.p3)
        self.approve(self.count, self.manager, accept={p3.pk})
        self.assertEqual((self.floor_qty(self.p1), self.floor_qty(self.p3)), (10, 9))
        self.assertEqual(self.line(self.count, self.p1).decision, 'skipped')

    def test_reason_is_required_for_every_applied_variance(self):
        with self.assertRaisesMessage(StockCountError, 'enter a reason'):
            self.approve(self.count, self.manager, default_reason='')
        self.assertEqual(self.floor_qty(self.p1), 10)
        self.count.refresh_from_db()
        self.assertEqual(self.count.status, Status.SUBMITTED)
        with self.assertRaisesMessage(StockCountError, 'longer than'):
            self.approve(self.count, self.manager, default_reason='x' * 501)

    def test_double_approve_is_a_no_op(self):
        self.approve(self.count, self.manager)
        before = StockMovement.objects.count()
        with self.assertRaises(StockCountAlreadyApproved):
            self.approve(self.count, self.manager)
        self.client.force_login(self.manager)
        response = self.client.post(reverse('stock_count_review', args=[self.count.pk]), {
            'expected_status': 'Submitted', 'accept': [str(l.pk) for l in self.count.lines.all()],
            'default_reason': 'again',
        }, follow=True)
        self.assertContains(response, 'already approved')
        self.assertEqual(StockMovement.objects.count(), before)
        self.assertEqual(self.floor_qty(self.p1), 8)

    def test_stale_expected_status_is_refused(self):
        with self.assertRaises(StockCountStaleError):
            approve_stock_count(
                count_id=self.count.pk, actor=self.manager, expected_status='Counting',
                accepted=set(self.count.lines.values_list('pk', flat=True)), reasons={},
                default_reason='x',
            )
        self.assertEqual(self.floor_qty(self.p1), 10)

    def test_client_posted_quantities_are_ignored(self):
        self.client.force_login(self.manager)
        p1 = self.line(self.count, self.p1)
        self.client.post(reverse('stock_count_review', args=[self.count.pk]), {
            'expected_status': 'Submitted', 'accept': [str(p1.pk)], 'default_reason': 'x',
            f'expected_qty_{p1.pk}': '1000', f'counted_{p1.pk}': '1000', 'variance': '500',
        })
        self.assertEqual(self.floor_qty(self.p1), 8)

    def test_reject_and_cancel_leave_stock_unchanged(self):
        with self.assertRaisesMessage(StockCountError, 'reason'):
            reject_stock_count(count_id=self.count.pk, actor=self.manager, expected_status='Submitted', reason=' ')
        reject_stock_count(count_id=self.count.pk, actor=self.manager, expected_status='Submitted', reason='Recount next week')
        self.count.refresh_from_db()
        self.assertEqual((self.count.status, self.count.open_slot), (Status.REJECTED, None))
        other = self.start(location=Batch.Location.WAREHOUSE)
        self.count_values(other, self.staff, {self.p1: 1})
        # Another Staff member did not start it, so cannot cancel it.
        with self.assertRaises(StockCountError):
            cancel_stock_count(count_id=other.pk, actor=self.staff2)
        cancel_stock_count(count_id=other.pk, actor=self.manager, reason='Wrong section')
        self.assertEqual((self.floor_qty(self.p1), self.floor_qty(self.p3)), (10, 7))
        self.assertEqual(WarehouseStock.objects.get(product=self.p1).quantity_on_hand, 40)
        self.assertFalse(StockMovement.objects.filter(movement_type='Stock Count').exists())
        self.assertEqual(
            list(self.count.events.values_list('to_status', flat=True)),
            ['Draft', 'Counting', 'Submitted', 'Rejected'],
        )


class DriftTests(_Fixture, TestCase):
    """Rule: target = on hand now + (counted - snapshot). Movements after a
    line was counted are kept; movements between its snapshot and its count
    force a recount."""

    def setUp(self):
        self.build()
        self.count = self.start()

    def test_sales_after_the_count_are_kept(self):
        self.count_values(self.count, self.staff, {self.p1: 8})   # snapshot 10, counted 8
        self._sell(self.p1, 3)                                    # on hand 10 -> 7
        self.submit(self.count, self.staff)
        self.approve(self.count, self.manager)
        # 7 on hand + (8 - 10) = 5 — not reset to the stale 8.
        self.assertEqual(self.floor_qty(self.p1), 5)
        self.assertEqual(self.floor_batch_total(self.p1), 5)
        line = self.line(self.count, self.p1)
        self.assertEqual((line.system_qty_at_approval, line.applied_change), (7, -2))

    def test_movement_during_the_count_forces_a_recount(self):
        self._sell(self.p1, 2)                                    # after snapshot, before count
        self.count_values(self.count, self.staff, {self.p1: 8, self.p3: 6})
        self.submit(self.count, self.staff)
        with self.assertRaisesMessage(StockCountError, 'while it was being counted'):
            self.approve(self.count, self.manager)
        self.assertEqual(self.floor_qty(self.p1), 8)              # only the sale moved it
        self.assertFalse(StockMovement.objects.filter(movement_type='Stock Count').exists())
        # Leaving it out applies the rest.
        p3 = self.line(self.count, self.p3)
        self.approve(self.count, self.manager, accept={p3.pk})
        self.assertEqual((self.floor_qty(self.p1), self.floor_qty(self.p3)), (8, 6))

    def test_recount_re_snapshots_and_clears_the_line(self):
        self._sell(self.p1, 2)
        self.count_values(self.count, self.staff, {self.p1: 7})
        self.submit(self.count, self.staff)
        p1 = self.line(self.count, self.p1)
        count, n = recount_stock_count(
            count_id=self.count.pk, actor=self.manager, expected_status='Submitted', line_ids=[p1.pk],
        )
        self.assertEqual((count.status, n), (Status.COUNTING, 1))
        p1.refresh_from_db()
        self.assertEqual((p1.expected_qty, p1.counted_qty, p1.counted_by), (8, None, None))
        self.count_values(count, self.staff, {self.p1: 7})
        self.submit(count, self.staff)
        self.approve(count, self.manager)
        self.assertEqual(self.floor_qty(self.p1), 7)

    def test_review_screen_shows_the_same_assessment(self):
        self._sell(self.p1, 2)
        self.count_values(self.count, self.staff, {self.p1: 8, self.p3: 9})
        self.submit(self.count, self.staff)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('stock_count_review', args=[self.count.pk]))
        self.assertContains(response, 'Moved -2 while counting')
        self.assertContains(response, 'Approve and Apply')

    def test_negative_result_is_refused_not_clamped(self):
        self.count_values(self.count, self.staff, {self.p1: 2})   # variance -8
        self._sell(self.p1, 5)                                    # on hand 5, 5 - 8 < 0
        self.submit(self.count, self.staff)
        with self.assertRaisesMessage(StockCountError, 'below zero'):
            self.approve(self.count, self.manager)
        self.assertEqual(self.floor_qty(self.p1), 5)


class WarehouseCountTests(_Fixture, TestCase):
    def setUp(self):
        self.build()

    def test_warehouse_variance_goes_through_the_same_path(self):
        count = self.start(location=Batch.Location.WAREHOUSE)
        self.count_values(count, self.staff, {self.p1: 37})
        self.submit(count, self.staff)
        self.approve(count, self.manager)
        stock = WarehouseStock.objects.get(product=self.p1)
        self.assertEqual(stock.quantity_on_hand, 37)
        movement = StockMovement.objects.get(movement_type='Stock Count')
        self.assertEqual(
            (movement.warehouse_stock, movement.location_type, movement.quantity_change),
            (stock, 'Warehouse', -3),
        )
        self.assertEqual(sum(Batch.objects.filter(
            product=self.p1, branch=self.a, location='Warehouse',
        ).values_list('quantity', flat=True)), 37)
        self.assertEqual(self.floor_qty(self.p1), 10)

    def test_unexpected_item_with_no_row_creates_one(self):
        count = self.start()
        self.client.force_login(self.staff)
        self.client.post(reverse('stock_count_count', args=[count.pk]), {
            'action': 'add', 'add_product_id': str(self.p4.pk),
        })
        self.count_values(count, self.staff, {self.p4: 4})
        self.submit(count, self.staff)
        self.approve(count, self.manager)
        self.assertEqual(self.floor_qty(self.p4), 4)
        self.assertEqual(self.floor_batch_total(self.p4), 4)


class RegistryTests(TestCase):
    URL_NAMES = (
        'stock_count_list', 'stock_count_detail', 'stock_count_start', 'stock_count_count',
        'stock_count_scan', 'stock_count_review', 'stock_count_reject', 'stock_count_recount',
        'stock_count_cancel',
    )

    def test_every_route_belongs_to_the_stock_count_module(self):
        for name in self.URL_NAMES:
            self.assertEqual(URL_NAME_TO_MODULE[name], STOCK_COUNT, name)
            reverse(name, args=[1]) if name not in ('stock_count_list', 'stock_count_start') else reverse(name)
        self.assertEqual(required_action_for_request('stock_count_detail', 'GET'), VIEW)
        self.assertEqual(required_action_for_request('stock_count_scan', 'POST'), ADD)
        self.assertEqual(required_action_for_request('stock_count_review', 'POST'), EDIT)
        self.assertEqual(required_action_for_request('stock_count_cancel', 'POST'), DELETE)

    def test_role_defaults(self):
        manager = role_permission_defaults('BRANCH_MANAGER')
        staff = role_permission_defaults('STAFF')
        self.assertIn(STOCK_COUNT, manager['allowed_modules'])
        self.assertIn(STOCK_COUNT, staff['allowed_modules'])
        self.assertEqual(manager['module_actions'][STOCK_COUNT], [ADD, EDIT, DELETE])
        self.assertEqual(staff['module_actions'][STOCK_COUNT], [ADD, DELETE])
        self.assertNotIn(STOCK_COUNT, role_permission_defaults('SUPPLIER')['allowed_modules'])

    def test_stock_count_is_not_offline_queueable(self):
        from offline_sync.registry import OPERATIONS
        url_names = {spec.url_name for spec in OPERATIONS.values()}
        self.assertFalse({n for n in url_names if n.startswith('stock_count')})


class ApproveInAutocommitTests(_Fixture, TransactionTestCase):
    """No wrapping transaction: a select_for_update() outside atomic() would
    raise TransactionManagementError here, which TestCase hides."""

    def setUp(self):
        self.build()

    def test_full_flow_through_the_views(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('stock_count_start'), {'location': 'Floor'})
        count = StockCount.objects.get()
        p1 = self.line(count, self.p1)
        self.client.post(reverse('stock_count_scan', args=[count.pk]), {'code': '4800001'})
        self.client.post(reverse('stock_count_count', args=[count.pk]), {
            f'counted_{p1.pk}': '6', f'orig_{p1.pk}': '', 'action': 'submit',
            'uncounted_policy': 'leave',
        })
        count.refresh_from_db()
        self.assertEqual(count.status, Status.SUBMITTED)

        self.client.force_login(self.manager)
        response = self.client.post(reverse('stock_count_review', args=[count.pk]), {
            'expected_status': 'Submitted', 'accept': [str(p1.pk)], 'default_reason': 'Shrinkage',
        })
        self.assertRedirects(response, reverse('stock_count_detail', args=[count.pk]))
        self.assertEqual(self.floor_qty(self.p1), 6)
        # A second click lands on the already-approved no-op.
        self.client.post(reverse('stock_count_review', args=[count.pk]), {
            'expected_status': 'Submitted', 'accept': [str(p1.pk)], 'default_reason': 'Shrinkage',
        })
        self.assertEqual(StockMovement.objects.filter(movement_type='Stock Count').count(), 1)
        self.assertEqual(self.floor_qty(self.p1), 6)

    def test_recount_reject_and_cancel_in_autocommit(self):
        count = self.start()
        self.count_values(count, self.staff, {self.p1: 9})
        self.submit(count, self.staff)
        recount_stock_count(count_id=count.pk, actor=self.manager, expected_status='Submitted',
                            line_ids=[self.line(count, self.p1).pk])
        self.count_values(count, self.staff, {self.p1: 9})
        self.submit(count, self.staff)
        reject_stock_count(count_id=count.pk, actor=self.manager, expected_status='Submitted', reason='No')
        second = self.start()
        cancel_stock_count(count_id=second.pk, actor=self.staff)
        self.assertEqual(self.floor_qty(self.p1), 10)
