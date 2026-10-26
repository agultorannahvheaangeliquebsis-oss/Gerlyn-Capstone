"""Who can see and file what: role and branch scoping of the list, detail and
photos; the list filters; the module permission wiring; the entry points; and
the Damaged Goods section of the Inventory Report.

    python manage.py test damage_reports.tests_access
"""
import importlib
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import BranchManagerProfile, Notification, User
from core import module_permissions as mp
from core.notifications import EVENT_MATRIX
from damage_reports import reporting, services
from damage_reports.models import DamageReport
from damage_reports.testing import FLOOR, WAREHOUSE, DamageFixtures, png

Status = DamageReport.Status
WRITE_OFF = DamageReport.Resolution.WRITE_OFF.value


class ScopedFixtures(DamageFixtures):
    """Reports in both branches from several people."""

    def make_reports(self):
        self.by_staff = self.file_report(self.staff, description='Filed by staff.')
        self.by_coworker = self.file_report(
            self.coworker, quantity='2', description='Filed by the coworker.')
        theirs = self.add_stock(self.other_branch, self.product, FLOOR, 20)
        self.theirs = self.file_report(
            self.other_staff, batch_id=theirs.pk, quantity='3', description='Other branch.')
        self.txn, self.item = self.make_order()
        self.by_customer = self.file_customer_report(self.txn, self.item)

    def listed(self, user, **params):
        self.client.force_login(user)
        response = self.client.get(reverse('damage_report_list'), params)
        self.assertEqual(response.status_code, 200)
        return {r.pk for r in response.context['reports']}, response


class ListScopingTests(ScopedFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.make_reports()

    def test_the_owner_sees_every_branch(self):
        ids, _ = self.listed(self.owner, range='all')
        self.assertEqual(ids, {
            self.by_staff.pk, self.by_coworker.pk, self.theirs.pk, self.by_customer.pk})

    def test_a_branch_manager_sees_only_their_branch_including_customer_reports(self):
        ids, _ = self.listed(self.manager, range='all')
        self.assertEqual(ids, {self.by_staff.pk, self.by_coworker.pk, self.by_customer.pk})
        ids, _ = self.listed(self.other_manager, range='all')
        self.assertEqual(ids, {self.theirs.pk})

    def test_staff_see_only_the_reports_they_filed(self):
        ids, _ = self.listed(self.staff, range='all')
        self.assertEqual(ids, {self.by_staff.pk})
        ids, _ = self.listed(self.coworker, range='all')
        self.assertEqual(ids, {self.by_coworker.pk})
        ids, _ = self.listed(self.other_staff, range='all')
        self.assertEqual(ids, {self.theirs.pk})

    def test_a_branch_filter_cannot_widen_a_managers_view(self):
        ids, _ = self.listed(self.manager, range='all', branch=self.other_branch.pk)
        self.assertEqual(ids, {self.by_staff.pk, self.by_coworker.pk, self.by_customer.pk})

    def test_an_unassigned_manager_sees_nothing_not_everything(self):
        BranchManagerProfile.objects.filter(user=self.manager).update(branch=None)
        manager = User.objects.get(pk=self.manager.pk)
        ids, response = self.listed(manager, range='all')
        self.assertEqual(ids, set())
        self.assertEqual(response.context['awaiting_total'], 0)

    def test_a_customer_and_a_visitor_cannot_open_the_list(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse('damage_report_list')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('damage_report_list')).status_code, 302)

    def test_the_visibility_helper_agrees(self):
        qs = DamageReport.objects.all()
        self.assertEqual(services.reports_visible_to(self.owner, qs).count(), 4)
        self.assertEqual(services.reports_visible_to(self.manager, qs).count(), 3)
        self.assertEqual(services.reports_visible_to(self.staff, qs).count(), 1)
        self.assertEqual(services.reports_visible_to(self.customer, qs).count(), 0)
        self.assertEqual(services.reports_visible_to(None, qs).count(), 0)


class ListFilterTests(ScopedFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.make_reports()
        self.coffee = self.add_stock(self.branch, self.product2, FLOOR, 10)
        self.coffee_report = self.file_report(
            self.staff, product_id=self.product2.pk, batch_id=self.coffee.pk, quantity='1',
            description='Coffee box torn.')
        services.reject_report(self.by_coworker.pk, self.manager, reason='No.')
        services.start_review(self.by_staff.pk, self.manager)

    def test_status_filter_and_tab_counts(self):
        ids, response = self.listed(self.manager, range='all', status='Rejected')
        self.assertEqual(ids, {self.by_coworker.pk})
        counts = response.context['counts']
        self.assertEqual(counts['Rejected'], 1)
        self.assertEqual(counts['Under Review'], 1)
        self.assertEqual(counts['Pending'], 2)           # customer + coffee
        self.assertEqual(response.context['total_count'], 4)   # tabs ignore the status filter

    def test_product_filter(self):
        ids, _ = self.listed(self.manager, range='all', product=self.product2.pk)
        self.assertEqual(ids, {self.coffee_report.pk})

    def test_branch_filter_for_the_owner(self):
        ids, _ = self.listed(self.owner, range='all', branch=self.other_branch.pk)
        self.assertEqual(ids, {self.theirs.pk})

    def test_search_by_number_product_order_or_reporter(self):
        for term, expected in (
            (self.by_staff.report_number, {self.by_staff.pk}),
            ('coffee', {self.coffee_report.pk}),
            (self.txn.order_number, {self.by_customer.pk}),
            ('dr-staff-b', {self.by_coworker.pk}),
        ):
            with self.subTest(term=term):
                ids, _ = self.listed(self.manager, range='all', q=term)
                self.assertEqual(ids, expected)

    def test_date_range_filter(self):
        DamageReport.objects.filter(pk=self.by_staff.pk).update(
            created_at=timezone.now() - timedelta(days=45))
        ids, _ = self.listed(self.manager)                       # default: last 30 days
        self.assertNotIn(self.by_staff.pk, ids)
        ids, _ = self.listed(self.manager, range='all')
        self.assertIn(self.by_staff.pk, ids)
        day = (timezone.localdate() - timedelta(days=45)).isoformat()
        ids, _ = self.listed(self.manager, range='day', date=day)
        self.assertEqual(ids, {self.by_staff.pk})

    def test_old_open_reports_are_still_flagged_even_when_the_range_hides_them(self):
        DamageReport.objects.filter(pk=self.coffee_report.pk).update(
            created_at=timezone.now() - timedelta(days=60))
        _, response = self.listed(self.manager)
        self.assertContains(response, 'waiting for a decision')
        # by_staff (review), customer + coffee (pending); the rejected one is not open.
        self.assertEqual(response.context['awaiting_total'], 3)

    def test_staff_do_not_get_the_manager_banner(self):
        _, response = self.listed(self.staff, range='all')
        self.assertEqual(response.context['awaiting_total'], 0)
        self.assertNotContains(response, 'waiting for a decision')

    def test_status_tabs_keep_the_other_filters(self):
        _, response = self.listed(self.manager, range='all', product=self.product2.pk)
        self.assertIn(f'product={self.product2.pk}', response.context['filter_qs'])
        self.assertIn('range=all', response.context['filter_qs'])

    def test_the_list_paginates(self):
        self.client.force_login(self.manager)
        for i in range(16):
            self.file_report(self.staff, quantity='1', description=f'More {i}',
                             batch_id='', photos=[])
            if i % 3 == 0:
                continue
        response = self.client.get(reverse('damage_report_list'), {'range': 'all'})
        self.assertEqual(len(response.context['reports']), 15)
        self.assertTrue(response.context['page_obj'].has_next())


class DetailAndPhotoAccessTests(ScopedFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.make_reports()

    def detail(self, user, report):
        self.client.force_login(user)
        return self.client.get(reverse('damage_report_detail', args=[report.pk]))

    def photo(self, user, report):
        self.client.force_login(user)
        photo = report.photos.first()
        return self.client.get(reverse('damage_report_photo', args=[report.pk, photo.pk]))

    def test_who_can_open_a_staff_report(self):
        for user, status in (
            (self.staff, 200), (self.manager, 200), (self.owner, 200),
            (self.coworker, 404),          # same branch, someone else's report
            (self.other_staff, 404), (self.other_manager, 404),
            (self.customer, 403),
        ):
            with self.subTest(user=user.username):
                self.assertEqual(self.detail(user, self.by_staff).status_code, status)

    def test_who_can_open_its_photos(self):
        for user, status in (
            (self.staff, 200), (self.manager, 200), (self.owner, 200),
            (self.coworker, 404), (self.other_manager, 404), (self.customer, 403),
        ):
            with self.subTest(user=user.username):
                self.assertEqual(self.photo(user, self.by_staff).status_code, status)

    def test_a_photo_id_from_another_report_does_not_leak(self):
        foreign = self.theirs.photos.first()
        self.client.force_login(self.manager)
        url = reverse('damage_report_photo', args=[self.by_staff.pk, foreign.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        direct = reverse('damage_report_photo', args=[self.theirs.pk, foreign.pk])
        self.assertEqual(self.client.get(direct).status_code, 404)

    def test_photos_are_served_as_the_image_they_are_and_never_cached(self):
        response = self.photo(self.manager, self.by_staff)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('no-store', response['Cache-Control'])

    def test_the_detail_page_shows_the_gallery_and_the_decision_panel_to_a_manager(self):
        page = self.detail(self.manager, self.by_staff)
        photo = self.by_staff.photos.first()
        self.assertContains(page, reverse('damage_report_photo', args=[self.by_staff.pk, photo.pk]))
        self.assertContains(page, 'Approve report')
        self.assertContains(page, 'Reject report')
        self.assertContains(page, 'Start review')
        self.assertContains(page, 'Removes 5 units of Canned Sardines 155g from floor stock')

    def test_the_reporter_sees_their_report_but_no_decision_controls(self):
        page = self.detail(self.staff, self.by_staff)
        self.assertContains(page, self.by_staff.report_number)
        self.assertContains(page, 'Waiting for a manager')
        self.assertNotContains(page, 'Approve report')
        self.assertNotContains(page, 'Reject report')

    def test_the_detail_page_renders_inside_the_modal_overlay_too(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse('damage_report_detail', args=[self.by_staff.pk]), {'modal': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.by_staff.report_number)

    def test_an_approved_report_offers_resolve_and_links_its_slip(self):
        services.approve_report(self.by_staff.pk, self.manager, resolution=WRITE_OFF)
        page = self.detail(self.manager, self.by_staff)
        self.by_staff.refresh_from_db()
        self.assertContains(page, 'Mark as resolved')
        self.assertContains(page, self.by_staff.withdrawal_slip.slip_number)
        self.assertNotContains(page, 'Approve report')

    def test_a_report_for_a_deleted_product_survives_with_its_name(self):
        self.product.delete()
        self.by_staff.refresh_from_db()
        self.assertIsNone(self.by_staff.product)
        page = self.detail(self.manager, self.by_staff)
        self.assertContains(page, 'Canned Sardines 155g')
        self.assertEqual(
            [r.value for r in services.allowed_resolutions(self.by_staff)], ['none'])


class ModulePermissionTests(ScopedFixtures, TestCase):
    def setUp(self):
        self.make_world()
        self.make_reports()

    def test_the_module_is_registered_with_its_routes(self):
        self.assertIn(mp.DAMAGE_REPORTS, mp.ALL_MODULE_CODES)
        self.assertEqual(mp.MODULE_REGISTRY[mp.DAMAGE_REPORTS][0], 'Damage Reports')
        expected = {
            'damage_report_list': 'view', 'damage_report_detail': 'view',
            'damage_report_photo': 'view', 'damage_report_create': 'add',
            'damage_report_review': 'edit', 'damage_report_approve': 'edit',
            'damage_report_reject': 'edit', 'damage_report_resolve': 'edit',
        }
        args = {
            'damage_report_list': [], 'damage_report_create': [],
            'damage_report_photo': [1, 1],
        }
        for url_name, action in expected.items():
            with self.subTest(url=url_name):
                self.assertEqual(mp.URL_NAME_TO_MODULE[url_name], mp.DAMAGE_REPORTS)
                self.assertEqual(mp.required_action_for_request(url_name, 'POST'), action)
                self.assertTrue(reverse(url_name, args=args.get(url_name, [1])))

    def test_role_defaults(self):
        self.assertEqual(self.staff.module_actions['damage_reports'], ['add'])
        self.assertEqual(self.manager.module_actions['damage_reports'], ['add', 'edit'])
        for user in (self.staff, self.manager):
            self.assertIn('damage_reports', user.allowed_modules)
        self.assertTrue(mp.user_can_perform_module_action(self.owner, 'damage_reports', 'edit'))
        self.assertFalse(mp.user_can_perform_module_action(self.staff, 'damage_reports', 'edit'))

    def test_owner_can_see_the_row_in_the_permission_matrix(self):
        rows = mp.permission_rows(['damage_reports'], {'damage_reports': ['add']})
        row = next(r for r in rows if r['code'] == 'damage_reports')
        self.assertEqual(row['label'], 'Damage Reports')
        self.assertTrue(row['view'] and row['add'])
        self.assertFalse(row['edit'])
        self.assertTrue(row['supports_add'] and row['supports_edit'])
        self.assertFalse(row['supports_delete'])

    def test_staff_without_the_module_are_locked_out_of_everything(self):
        User.objects.filter(pk=self.staff.pk).update(allowed_modules=['sales_pos'])
        staff = User.objects.get(pk=self.staff.pk)
        self.client.force_login(staff)
        photo = self.by_staff.photos.first()
        for url in (
            reverse('damage_report_list'),
            reverse('damage_report_create'),
            reverse('damage_report_detail', args=[self.by_staff.pk]),
            reverse('damage_report_photo', args=[self.by_staff.pk, photo.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_sidebar_link_follows_the_module(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('inventory_list'))
        self.assertContains(page, reverse('damage_report_list'))
        User.objects.filter(pk=self.staff.pk).update(allowed_modules=['floor_stock'])
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        page = self.client.get(reverse('inventory_list'))
        self.assertNotContains(page, reverse('damage_report_list'))

    def test_the_data_migration_grants_existing_accounts_the_module(self):
        migration = importlib.import_module(
            'damage_reports.migrations.0002_grant_damage_reports_module')
        legacy = {
            'allowed_modules': ['sales_pos', 'floor_stock'],
            'module_actions': {'sales_pos': ['add']},
        }
        User.objects.filter(pk__in=[self.staff.pk, self.manager.pk]).update(**legacy)

        migration.grant(django_apps, None)

        staff = User.objects.get(pk=self.staff.pk)
        manager = User.objects.get(pk=self.manager.pk)
        self.assertIn('damage_reports', staff.allowed_modules)
        self.assertEqual(staff.module_actions['damage_reports'], ['add'])
        self.assertEqual(manager.module_actions['damage_reports'], ['add', 'edit'])
        self.assertEqual(staff.module_actions['sales_pos'], ['add'])        # untouched
        self.assertEqual(User.objects.get(pk=self.customer.pk).allowed_modules,
                         self.customer.allowed_modules)

        migration.grant(django_apps, None)                                  # idempotent
        self.assertEqual(
            User.objects.get(pk=self.staff.pk).allowed_modules.count('damage_reports'), 1)

        migration.revoke(django_apps, None)
        self.assertNotIn('damage_reports', User.objects.get(pk=self.staff.pk).allowed_modules)

    def test_the_data_migration_does_not_widen_an_owner_narrowed_account(self):
        migration = importlib.import_module(
            'damage_reports.migrations.0002_grant_damage_reports_module')
        User.objects.filter(pk=self.staff.pk).update(
            allowed_modules=['floor_stock'], module_actions={'damage_reports': []})
        migration.grant(django_apps, None)
        staff = User.objects.get(pk=self.staff.pk)
        # An explicit empty grant already in place is kept; only the View was missing.
        self.assertEqual(staff.module_actions['damage_reports'], [])


class EntryPointTests(DamageFixtures, TestCase):
    def setUp(self):
        self.make_world()

    def test_floor_stock_has_the_button_and_a_per_batch_link(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('inventory_list'))
        self.assertContains(page, 'Report Damaged Product')
        self.assertContains(
            page, f'{reverse("damage_report_create")}?batch={self.floor_batch.pk}'
                  f'&amp;branch={self.branch.pk}')

    def test_warehouse_stock_has_the_button_and_a_per_batch_link(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('warehouse_stock_list'))
        self.assertContains(page, 'Report Damaged Product')
        self.assertContains(page, f'?batch={self.wh_batch.pk}&amp;branch={self.branch.pk}')

    def test_the_owner_keeps_the_edit_action_beside_the_new_one(self):
        # Editing a reorder level is an Owner action by default; the new link
        # sits next to it instead of replacing it.
        self.client.force_login(self.owner)
        page = self.client.get(reverse('inventory_list'), {'branch': self.branch.pk})
        self.assertContains(page, 'Edit reorder level')
        self.assertContains(page, 'Report damaged product')

    def test_an_account_that_cannot_edit_still_gets_the_actions_column(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('inventory_list'))
        self.assertNotContains(page, 'Edit reorder level')
        self.assertContains(page, '<th class="end">Actions</th>')

    def test_without_the_add_grant_the_buttons_disappear(self):
        User.objects.filter(pk=self.staff.pk).update(module_actions={'damage_reports': []})
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        for name in ('inventory_list', 'warehouse_stock_list'):
            with self.subTest(page=name):
                self.assertNotContains(self.client.get(reverse(name)), 'Report Damaged Product')

    def test_a_received_purchase_order_offers_a_report_for_its_own_goods(self):
        po = self.make_po()
        self.add_stock(self.branch, self.product, WAREHOUSE, 6, procurement=po)
        self.client.force_login(self.manager)
        page = self.client.get(reverse('procurement_detail', args=[po.pk]))
        self.assertContains(page, 'Report damaged goods')
        self.assertContains(page, f'{reverse("damage_report_create")}?procurement={po.pk}')

    def test_the_purchase_order_form_lists_only_that_orders_lots(self):
        po = self.make_po()
        lot = self.add_stock(self.branch, self.product, WAREHOUSE, 6, procurement=po)
        self.client.force_login(self.staff)
        page = self.client.get(f'{reverse("damage_report_create")}?procurement={po.pk}')
        self.assertContains(page, 'Reporting goods from purchase order')
        self.assertContains(page, lot.batch_number)
        self.assertNotContains(page, self.floor_batch.batch_number)
        self.assertContains(page, f'name="procurement_id" value="{po.pk}"')
        self.assertContains(page, '<option value="arrival" selected>')

    def test_filing_from_a_purchase_order_links_it_and_defaults_to_arrival_damage(self):
        po = self.make_po()
        lot = self.add_stock(self.branch, self.product, WAREHOUSE, 6, procurement=po)
        self.client.force_login(self.staff)
        self.client.post(reverse('damage_report_create'), self.post_data(
            stock_key=f'{WAREHOUSE}|{self.product.pk}', batch_id=str(lot.pk), quantity='2',
            damage_type='arrival', procurement_id=po.pk))
        report = DamageReport.objects.get()
        self.assertEqual(report.procurement, po)
        self.assertEqual(report.damage_type, 'arrival')

    def test_a_purchase_order_from_another_branch_is_ignored_with_a_message(self):
        other_po = self.make_po(branch=self.other_branch)
        self.client.force_login(self.staff)
        page = self.client.get(f'{reverse("damage_report_create")}?procurement={other_po.pk}')
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Reporting goods from purchase order')
        self.assertContains(page, 'not one you can report against')

    def test_the_receive_modal_points_at_the_report_form(self):
        po = self.make_po(status='Incoming')
        self.client.force_login(self.staff)
        page = self.client.get(reverse('delivery_list'), {'receive': po.pk})
        self.assertContains(page, 'Goods damaged on arrival?')
        self.assertContains(page, f'{reverse("damage_report_create")}?procurement={po.pk}')


class StatusPillTests(SimpleTestCase):
    def pill(self, status, **ctx):
        return render_to_string('partials/_damage_status_pill.html', {'status': status, **ctx})

    def test_colours_follow_the_design_status_table(self):
        for status, css in (
            ('Pending', 'pill-warn'), ('Under Review', 'pill-info'), ('Approved', 'pill-info'),
            ('Rejected', 'pill-danger'), ('Resolved', 'pill-ok'),
        ):
            with self.subTest(status=status):
                self.assertIn(css, self.pill(status))

    def test_customers_get_plain_words(self):
        self.assertIn('Not approved', self.pill('Rejected', customer=True))
        self.assertIn('Being reviewed', self.pill('Under Review', customer=True))
        self.assertIn('Received', self.pill('Pending', customer=True))
        self.assertIn('Rejected', self.pill('Rejected'))


class NotificationMatrixTests(SimpleTestCase):
    def test_both_events_are_declared_and_stored_choices_exist(self):
        from accounts.models import Notification
        for event in ('damage_report_submitted', 'damage_report_status'):
            with self.subTest(event=event):
                self.assertIn(event, EVENT_MATRIX)
                self.assertIn(event, Notification.EventType.values)
        self.assertEqual(EVENT_MATRIX['damage_report_submitted']['audiences'], ('manager',))
        self.assertEqual(EVENT_MATRIX['damage_report_status']['audiences'], ('reporter',))


class InventoryReportDamagedGoodsTests(ScopedFixtures, TestCase):
    """Quantity and estimated loss per product and branch, confirmed damage only."""

    def setUp(self):
        self.make_world()
        self.url = reverse('inventory_report')

    def approved(self, **kw):
        report = self.file_report(self.staff, **kw)
        services.approve_report(report.pk, self.manager, resolution=WRITE_OFF)
        return report

    def rows(self, user, **params):
        self.client.force_login(user)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.context['damage_rows'], response.context['damage_totals'], response

    def test_only_confirmed_damage_counts(self):
        self.approved(quantity='5')                                         # Approved
        resolved = self.approved(quantity='2', description='B')
        services.resolve_report(resolved.pk, self.manager)                  # Resolved
        self.file_report(self.staff, quantity='3', description='Pending')   # not counted
        under = self.file_report(self.staff, quantity='4', description='Review')
        services.start_review(under.pk, self.manager)                       # not counted
        rejected = self.file_report(self.staff, quantity='6', description='Rejected')
        services.reject_report(rejected.pk, self.manager, reason='No.')     # not counted

        rows, totals, _ = self.rows(self.manager)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['product'], row['branch'], row['reports'], row['units']),
                         ('Canned Sardines 155g', 'Tarcan', 2, 7))
        self.assertEqual(row['loss'], Decimal('140.00'))                    # 7 x 20.00
        self.assertEqual(totals, {'reports': 2, 'units': 7, 'loss': Decimal('140.00')})

    def test_one_row_per_product_and_branch_and_the_section_renders(self):
        self.approved(quantity='5')
        coffee = self.add_stock(self.branch, self.product2, FLOOR, 5)
        self.approved(product_id=self.product2.pk, batch_id=coffee.pk, quantity='2', description='C')
        theirs = self.add_stock(self.other_branch, self.product, FLOOR, 9)
        other = self.file_report(
            self.other_staff, batch_id=theirs.pk, quantity='4', description='D')
        services.approve_report(other.pk, self.other_manager, resolution=WRITE_OFF)

        rows, totals, response = self.rows(self.owner)

        self.assertEqual(
            sorted((r['branch'], r['product'], r['units'], str(r['loss'])) for r in rows),
            [('Makinabang', 'Canned Sardines 155g', 4, '80.00'),
             ('Tarcan', 'Canned Sardines 155g', 5, '100.00'),
             ('Tarcan', 'Coffee 3in1 (Box)', 2, '180.00')])
        self.assertEqual(totals['loss'], Decimal('360.00'))
        self.assertContains(response, 'Damaged Goods')
        self.assertContains(response, '₱360.00')
        self.assertContains(response, 'Estimated Loss')

    def test_customer_reported_units_are_their_own_column(self):
        txn, item = self.make_order(quantity=4)
        report = self.file_customer_report(txn, item, quantity='2')
        services.approve_report(
            report.pk, self.manager, resolution='refund', refund_method='Cash')
        self.approved(quantity='1')

        rows, _, _ = self.rows(self.manager)

        self.assertEqual((rows[0]['units'], rows[0]['customer_units']), (3, 2))

    def test_a_manager_sees_only_their_branch_and_an_owner_can_narrow(self):
        self.approved(quantity='5')
        theirs = self.add_stock(self.other_branch, self.product, FLOOR, 9)
        other = self.file_report(
            self.other_staff, batch_id=theirs.pk, quantity='4', description='D')
        services.approve_report(other.pk, self.other_manager, resolution=WRITE_OFF)

        rows, _, _ = self.rows(self.manager)
        self.assertEqual({r['branch'] for r in rows}, {'Tarcan'})
        rows, _, _ = self.rows(self.other_manager)
        self.assertEqual({r['branch'] for r in rows}, {'Makinabang'})
        rows, _, _ = self.rows(self.owner)
        self.assertEqual({r['branch'] for r in rows}, {'Tarcan', 'Makinabang'})
        rows, _, _ = self.rows(self.owner, branch=self.other_branch.pk)
        self.assertEqual({r['branch'] for r in rows}, {'Makinabang'})

    def test_an_unassigned_manager_gets_nothing(self):
        self.approved(quantity='5')
        BranchManagerProfile.objects.filter(user=self.manager).update(branch=None)
        rows, totals, _ = self.rows(User.objects.get(pk=self.manager.pk))
        self.assertEqual((list(rows), totals['units']), ([], 0))
        self.assertEqual(reporting.damaged_goods([])[0], [])

    def test_staff_cannot_open_the_report_at_all(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_the_loss_uses_the_cost_frozen_when_the_report_was_filed(self):
        report = self.approved(quantity='5')
        self.product.unit_price = Decimal('50.00')
        self.product.save()

        rows, totals, _ = self.rows(self.manager)

        self.assertEqual(report.estimated_unit_cost, Decimal('20.00'))
        self.assertEqual(totals['loss'], Decimal('100.00'))

    def test_the_period_filter(self):
        old = self.approved(quantity='5')
        DamageReport.objects.filter(pk=old.pk).update(
            reviewed_at=timezone.now() - timedelta(days=100))
        self.approved(quantity='2', description='Recent')

        for period, units in (('all', 7), ('365', 7), ('90', 2), ('30', 2), ('bogus', 7)):
            with self.subTest(period=period):
                _, totals, _ = self.rows(self.manager, damage_period=period)
                self.assertEqual(totals['units'], units)

    def test_an_empty_report_says_so(self):
        _, _, response = self.rows(self.manager)
        self.assertContains(response, 'No damaged goods in this period')

    def test_a_deleted_product_still_counts_under_its_remembered_name(self):
        self.approved(quantity='5')
        self.product.delete()
        rows, _, _ = self.rows(self.manager)
        self.assertEqual(rows[0]['product'], 'Canned Sardines 155g')
        self.assertEqual(rows[0]['units'], 5)

    def test_the_stock_figures_on_the_report_are_unchanged_by_the_new_section(self):
        self.approved(quantity='5')
        _, _, response = self.rows(self.manager)
        self.assertEqual(response.context['total_units'], 25)       # the 5 left the floor
