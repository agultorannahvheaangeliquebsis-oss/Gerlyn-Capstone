"""P10 - Alerts Center redirects and suggested actions.

What each alert row lets the viewer do, permission-gated; the tab hashes that
links use; the notification links every recipient can actually open; and the
Create PO prefill the alert links rely on.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from accounts.models import BranchManagerProfile, Notification, StaffProfile, User
from branches.models import Branch
from core.module_permissions import (
    ALERTS, DELIVERIES, FLOOR_STOCK, PRODUCTS, PROCUREMENT, WAREHOUSE_STOCK,
    default_module_actions,
)
from inventory.alert_actions import (
    ALERT_PANELS, ALERT_TAB_HASHES, overdue_order_url,
)
from inventory.models import Batch, Inventory, Product, WarehouseStock
from inventory.services import expire_due_batches
from procurement.models import Procurement, SupplierProduct

TODAY = dt.date.today()


def _resolves(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


class _Fixture(TestCase):
    """One branch with a low floor row for one product, plus the four kinds of
    viewer the gating has to tell apart."""

    def setUp(self):
        self.branch = Branch.objects.create(branch_name='Tarcan', location='Baliuag')
        self.other_branch = Branch.objects.create(branch_name='Makinabang', location='Baliuag')
        self.product = Product.objects.create(
            sku_code='SKU-A1', product_name='Rice', unit_price=Decimal('50.00'))
        self.floor = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=2, reorder_level=10)
        self.owner = User.objects.create_user(
            username='a-owner', password='pw', role=User.Role.OWNER)
        self.manager = self.make_user('a-manager', User.Role.BRANCH_MANAGER)
        self.staff = self.make_user('a-staff', User.Role.STAFF, extra_modules=[ALERTS])

    def make_user(self, username, role, extra_modules=(), modules=None, drop_actions=()):
        user = User.objects.create_user(username=username, password='pw', role=role)
        if role == User.Role.BRANCH_MANAGER:
            BranchManagerProfile.objects.filter(user=user).update(branch=self.branch)
        else:
            StaffProfile.objects.filter(user=user).update(branch=self.branch)
        user.refresh_from_db()
        if modules is not None:
            user.allowed_modules = list(modules)
        else:
            user.allowed_modules = list(dict.fromkeys([*user.allowed_modules, *extra_modules]))
        actions = default_module_actions()
        for module in drop_actions:
            actions[module] = []
        user.module_actions = actions
        user.save(update_fields=['allowed_modules', 'module_actions'])
        return user

    def warehouse_stock(self, quantity):
        return WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=self.product,
            quantity_on_hand=quantity, reorder_level=5)

    def page(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse('alerts_center'))
        self.assertEqual(response.status_code, 200, f'{user.username} could not open the page')
        return response

    def floor_row(self, user):
        return self.page(user).context['low_stock'][0]

    @staticmethod
    def kinds(row):
        return [a['kind'] for a in row['actions']]


class FloorLowActionTests(_Fixture):
    def test_warehouse_with_stock_offers_request_and_never_create_po(self):
        self.warehouse_stock(30)
        row = self.floor_row(self.owner)
        self.assertEqual(self.kinds(row), ['request', 'history'])
        request_url = f"{reverse('warehouse_stock_list')}?open_request=1&branch={self.branch.pk}"
        self.assertEqual(row['actions'][0]['url'], request_url)
        response = self.page(self.owner)
        self.assertContains(response, 'Request Stock to Floor')
        self.assertNotContains(response, reverse('procurement_add'))

    def test_empty_warehouse_offers_create_po_instead_of_request(self):
        self.warehouse_stock(0)
        row = self.floor_row(self.owner)
        self.assertEqual(self.kinds(row), ['po', 'history'])
        self.assertIn('needs a purchase order', row['note'])
        self.assertNotContains(self.page(self.owner), 'Request Stock to Floor')

    def test_no_warehouse_row_at_all_counts_as_empty(self):
        self.assertEqual(self.kinds(self.floor_row(self.owner)), ['po', 'history'])

    def test_a_warehouse_holding_only_expired_stock_counts_as_empty(self):
        self.warehouse_stock(8)
        Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.WAREHOUSE,
            quantity=8, received_date=TODAY - dt.timedelta(days=60),
            expiry_date=TODAY - dt.timedelta(days=1))
        self.assertEqual(self.kinds(self.floor_row(self.owner)), ['po', 'history'])

    def test_create_po_link_carries_branch_product_and_suggested_quantity(self):
        row = self.floor_row(self.owner)
        self.assertEqual(row['suggested_qty'], 18)  # 2 * reorder(10) - on hand(2)
        self.assertEqual(
            row['actions'][0]['url'],
            f"{reverse('procurement_add')}?branch={self.branch.pk}"
            f"&product={self.product.pk}&quantity=18")

    def test_request_needs_the_warehouse_stock_add_grant(self):
        self.warehouse_stock(30)
        view_only = self.make_user(
            'a-viewonly', User.Role.BRANCH_MANAGER, drop_actions=[WAREHOUSE_STOCK])
        row = self.floor_row(view_only)
        self.assertNotIn('request', self.kinds(row))
        self.assertNotContains(self.page(view_only), 'Request Stock to Floor')

    def test_create_po_is_owner_or_manager_only(self):
        # Manager: yes. Staff, even with Alerts access and every grant: no.
        self.assertIn('po', self.kinds(self.floor_row(self.manager)))
        staff_row = self.floor_row(self.staff)
        self.assertNotIn('po', self.kinds(staff_row))
        self.assertIn('needs a purchase order', staff_row['note'])  # says why, no dead button
        self.assertNotContains(self.page(self.staff), reverse('procurement_add'))

    def test_create_po_needs_the_procurement_add_grant_too(self):
        manager = self.make_user(
            'a-nopo', User.Role.BRANCH_MANAGER, drop_actions=[PROCUREMENT])
        self.assertNotIn('po', self.kinds(self.floor_row(manager)))


class HistoryLinkTests(_Fixture):
    def history(self, user):
        return next(a for a in self.floor_row(user)['actions'] if a['kind'] == 'history')

    def test_owner_history_is_all_time_and_branch_narrowed(self):
        self.assertEqual(
            self.history(self.owner)['url'],
            f"{reverse('stock_movement_history')}?product={self.product.pk}"
            f"&range=all&branch={self.branch.pk}")

    def test_manager_history_is_all_time_without_a_branch_parameter(self):
        self.assertEqual(
            self.history(self.manager)['url'],
            f"{reverse('stock_movement_history')}?product={self.product.pk}&range=all")

    def test_history_link_needs_the_product_master_list_view_grant(self):
        no_products = self.make_user(
            'a-noprod', User.Role.STAFF,
            modules=[FLOOR_STOCK, WAREHOUSE_STOCK, ALERTS])
        self.assertNotIn('history', self.kinds(self.floor_row(no_products)))
        self.assertNotContains(self.page(no_products), reverse('stock_movement_history'))

    def test_the_link_it_renders_opens_for_the_viewer_who_sees_it(self):
        self.client.force_login(self.manager)
        url = self.history(self.manager)['url']
        self.assertEqual(self.client.get(url).status_code, 200)


class WarehouseLowAndExpiryActionTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.ws = self.warehouse_stock(3)  # at/below its reorder level of 5

    def test_warehouse_low_row_offers_create_po_with_prefill(self):
        row = self.page(self.owner).context['low_warehouse_stock'][0]
        self.assertEqual(self.kinds(row), ['po', 'history'])
        self.assertEqual(row['suggested_qty'], 7)
        self.assertIn(f'branch={self.branch.pk}&product={self.product.pk}&quantity=7',
                      row['actions'][0]['url'])
        self.assertNotIn('po', self.kinds(self.page(self.staff).context['low_warehouse_stock'][0]))

    def lot(self, location, days):
        return Batch.objects.create(
            product=self.product, branch=self.branch, location=location, quantity=4,
            received_date=TODAY - dt.timedelta(days=90),
            expiry_date=TODAY + dt.timedelta(days=days))

    def test_soon_and_approaching_tabs_have_an_action_column(self):
        self.lot(Batch.Location.WAREHOUSE, 10)
        self.lot(Batch.Location.WAREHOUSE, 60)
        response = self.page(self.owner)
        # Expired, Expiring Soon and Approaching all carry an Actions column
        # (the two low-stock tabs keep their singular "Action" header).
        self.assertEqual(response.content.decode().count('<th class="end">Actions</th>'), 3)
        for key in ('expiry_soon', 'expiry_approaching'):
            row = response.context[key][0]
            self.assertEqual(self.kinds(row), ['request', 'history'])
            self.assertEqual(row['actions'][0]['label'], 'Request to Floor')

    def test_a_floor_lot_expiring_offers_history_only(self):
        self.lot(Batch.Location.FLOOR, 10)
        row = self.page(self.owner).context['expiry_soon'][0]
        self.assertEqual(self.kinds(row), ['history'])

    def test_request_to_floor_is_gated_on_the_warehouse_add_grant(self):
        self.lot(Batch.Location.WAREHOUSE, 10)
        view_only = self.make_user(
            'a-viewonly2', User.Role.BRANCH_MANAGER, drop_actions=[WAREHOUSE_STOCK])
        self.assertEqual(self.kinds(self.page(view_only).context['expiry_soon'][0]), ['history'])

    def test_expired_rows_keep_write_off_and_gain_history(self):
        self.lot(Batch.Location.FLOOR, -3)
        response = self.page(self.owner)
        row = response.context['expiry_expired'][0]
        self.assertEqual(self.kinds(row), ['history'])
        self.assertContains(response, 'Write Off')
        # Write Off stays behind the Alerts delete grant.
        no_delete = self.make_user('a-nodelete', User.Role.STAFF,
                                   extra_modules=[ALERTS, PRODUCTS], drop_actions=[ALERTS])
        page = self.page(no_delete)
        self.assertNotContains(page, 'openWriteOffConfirm(')
        self.assertEqual(self.kinds(page.context['expiry_expired'][0]), ['history'])


class OverdueLinkTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.supplier = User.objects.create_user(
            username='a-supplier', password='pw', role=User.Role.SUPPLIER)
        past = TODAY - dt.timedelta(days=6)
        self.po = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, order_date=past,
            delivery_date=past, status=Procurement.Status.INCOMING, total_cost=0)

    def link(self, user):
        po = self.page(user).context['overdue_deliveries'][0]
        return po.action_url

    def test_owner_and_manager_go_to_the_purchase_order(self):
        expected = reverse('procurement_detail', args=[self.po.pk])
        self.assertEqual(self.link(self.owner), expected)
        self.assertEqual(self.link(self.manager), expected)

    def test_staff_go_to_the_delivery_page(self):
        # delivery_detail arrives with main's Staff delivery access; until this
        # branch has it, the Deliveries list is the honest fallback.
        expected = _resolves('delivery_detail', self.po.pk) or reverse('delivery_list')
        self.assertEqual(self.link(self.staff), expected)

    def test_a_manager_without_procurement_falls_back_to_deliveries(self):
        manager = self.make_user(
            'a-mgr2', User.Role.BRANCH_MANAGER,
            modules=[ALERTS, DELIVERIES, FLOOR_STOCK, WAREHOUSE_STOCK])
        expected = _resolves('delivery_detail', self.po.pk) or reverse('delivery_list')
        self.assertEqual(self.link(manager), expected)

    def test_no_procurement_and_no_deliveries_means_no_link(self):
        nobody = self.make_user('a-none', User.Role.STAFF, modules=[ALERTS])
        self.assertEqual(overdue_order_url(nobody, self.po), '')
        self.assertContains(self.page(nobody), 'gv-meta">—</span></td>')

    def test_the_rendered_link_is_a_page_the_viewer_can_open(self):
        for user in (self.owner, self.manager, self.staff):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                url = self.link(user)
                # The page test client can open it: 200, not a bounce.
                self.assertEqual(self.client.get(url).status_code, 200)


class TabHashTests(_Fixture):
    def test_every_hash_maps_to_a_real_panel(self):
        self.assertTrue(set(ALERT_TAB_HASHES.values()) <= set(ALERT_PANELS))
        self.assertEqual(set(ALERT_TAB_HASHES.values()), set(ALERT_PANELS))

    def test_the_missing_hashes_are_now_handled(self):
        self.assertEqual(ALERT_TAB_HASHES['expired'], 'expired')
        self.assertEqual(ALERT_TAB_HASHES['approaching'], 'approaching')
        # ...and the older spellings the dashboard still uses keep working.
        self.assertEqual(ALERT_TAB_HASHES['expiring'], 'expired')
        self.assertEqual(ALERT_TAB_HASHES['low'], 'floor')
        self.assertEqual(ALERT_TAB_HASHES['soon'], 'soon')
        self.assertEqual(ALERT_TAB_HASHES['warehouse'], 'warehouse')

    def test_the_page_serves_the_map_and_has_every_panel(self):
        response = self.page(self.owner)
        html = response.content.decode()
        self.assertIn('id="alert-tab-hashes"', html)
        self.assertEqual(response.context['alert_tab_hashes'], ALERT_TAB_HASHES)
        for panel in ALERT_PANELS:
            self.assertIn(f'id="panel-{panel}"', html)
            self.assertIn(f'id="tab-{panel}"', html)

    def test_every_hash_a_notification_uses_is_in_the_map(self):
        WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=self.product,
            quantity_on_hand=20, reorder_level=10)
        ws = WarehouseStock.objects.get()
        ws.quantity_on_hand = 4
        ws.save()
        self.floor.quantity_on_hand = 30
        self.floor.save()
        self.floor.quantity_on_hand = 3
        self.floor.save()
        hashes = {n.action_url.split('#')[1] for n in Notification.objects.exclude(
            action_url='') if '#' in n.action_url}
        self.assertTrue(hashes, 'no alert notification carried a #hash')
        self.assertLessEqual(hashes, set(ALERT_TAB_HASHES))


class NotificationLinkTests(_Fixture):
    """Every recipient the matrix selects can open the link they were given."""

    def setUp(self):
        super().setUp()
        # Staff who can see stock but NOT the Alerts Center - the bounced case.
        self.stock_only_staff = self.make_user('a-stock', User.Role.STAFF)
        self.assertNotIn(ALERTS, self.stock_only_staff.allowed_modules)

    def assert_every_recipient_can_open(self, event_type, expect_users, warehouse=None):
        rows = list(Notification.objects.filter(event_type=event_type))
        if warehouse is not None:  # low_stock covers floor and warehouse alike
            rows = [r for r in rows if ('(Warehouse)' in r.title) == warehouse]
        self.assertEqual({r.user.username for r in rows} >= set(expect_users), True,
                         f'{event_type}: expected {expect_users}, got '
                         f'{[r.user.username for r in rows]}')
        for row in rows:
            with self.subTest(event=event_type, user=row.user.username):
                self.assertTrue(row.action_url, 'recipient got no link at all')
                self.client.force_login(row.user)
                response = self.client.get(row.action_url.split('#')[0])
                self.assertEqual(response.status_code, 200,
                                 f'{row.user.username} bounced from {row.action_url}')
        return {r.user.username: r for r in rows}

    def test_warehouse_low_stock_link_opens_for_every_recipient(self):
        ws = WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=self.product,
            quantity_on_hand=20, reorder_level=10)
        ws.quantity_on_hand = 4
        ws.save()
        rows = self.assert_every_recipient_can_open(
            'low_stock', ['a-owner', 'a-manager', 'a-staff', 'a-stock'], warehouse=True)
        alerts = f"{reverse('alerts_center')}#warehouse"
        self.assertEqual(rows['a-owner'].action_url, alerts)
        self.assertEqual(rows['a-staff'].action_url, alerts)  # has Alerts access
        # No Alerts access: pointed at the Warehouse Stock page instead.
        self.assertEqual(rows['a-stock'].action_url, reverse('warehouse_stock_list'))
        self.assertEqual(rows['a-stock'].action_label, 'View Warehouse Stock')

    def test_floor_low_stock_link_opens_for_every_recipient(self):
        self.floor.quantity_on_hand = 30
        self.floor.save()
        self.floor.quantity_on_hand = 3
        self.floor.save()
        rows = self.assert_every_recipient_can_open(
            'low_stock', ['a-owner', 'a-manager', 'a-staff', 'a-stock'], warehouse=False)
        self.assertEqual(rows['a-owner'].action_url, f"{reverse('alerts_center')}#floor")
        self.assertEqual(rows['a-stock'].action_url, reverse('inventory_list'))

    def test_expiry_write_off_link_opens_for_every_recipient(self):
        WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=self.product,
            quantity_on_hand=6, reorder_level=1)
        for location, qty in ((Batch.Location.FLOOR, 2), (Batch.Location.WAREHOUSE, 6)):
            Batch.objects.create(
                product=self.product, branch=self.branch, location=location,
                quantity=qty, received_date=TODAY - dt.timedelta(days=60),
                expiry_date=TODAY - dt.timedelta(days=1))
        expire_due_batches(actor=None)
        rows = list(Notification.objects.filter(event_type='expiry_write_off'))
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(user=row.user.username, title=row.title):
                self.assertTrue(row.action_url)
                self.client.force_login(row.user)
                self.assertEqual(
                    self.client.get(row.action_url.split('#')[0]).status_code, 200)
        by_user = {(r.user.username, 'Floor' in r.title): r for r in rows}
        self.assertEqual(by_user[('a-owner', True)].action_url,
                         f"{reverse('alerts_center')}#expired")
        self.assertEqual(by_user[('a-stock', True)].action_url, reverse('inventory_list'))
        self.assertEqual(by_user[('a-stock', False)].action_url, reverse('warehouse_stock_list'))

    def test_near_expiry_link_opens_for_every_recipient(self):
        Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=3, received_date=TODAY - dt.timedelta(days=60),
            expiry_date=TODAY + dt.timedelta(days=10))
        rows = self.assert_every_recipient_can_open(
            'near_expiry', ['a-owner', 'a-manager', 'a-staff'])
        # Recipients are chosen by Alerts access, so a stock-only Staff is not one.
        self.assertNotIn('a-stock', rows)

    def test_a_recipient_who_can_open_nothing_gets_no_link_rather_than_a_dead_one(self):
        from core.notifications import _link_for_recipient
        nobody = self.make_user('a-nobody', User.Role.STAFF, modules=[])
        self.assertEqual(
            _link_for_recipient(nobody, [(ALERTS, '/x/', 'X'), (FLOOR_STOCK, '/y/', 'Y')]),
            ('', ''))


class CreatePoPrefillTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.supplier = self.make_supplier('a-sup-1')

    def make_supplier(self, name):
        supplier = User.objects.create_user(
            username=name, password='pw', role=User.Role.SUPPLIER)
        SupplierProduct.objects.create(
            supplier=supplier, product=self.product,
            current_cost_price=Decimal('40.00'),
            status=SupplierProduct.Status.APPROVED)
        return supplier

    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('procurement_add'), params)

    def test_owner_gets_branch_product_and_quantity_prefilled(self):
        response = self.get(self.owner, branch=self.branch.pk, product=self.product.pk, quantity=18)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['po_form_data']['branch'], str(self.branch.pk))
        self.assertEqual(response.context['po_restore_rows'],
                         [{'product_id': str(self.product.pk), 'quantity': 18}])
        self.assertContains(response, 'id="po-restore-rows"')

    def test_exactly_one_carrying_supplier_is_preselected(self):
        response = self.get(self.owner, branch=self.branch.pk, product=self.product.pk)
        self.assertEqual(response.context['po_form_data']['supplier'], str(self.supplier.pk))

    def test_several_carrying_suppliers_are_not_guessed_between(self):
        self.make_supplier('a-sup-2')
        response = self.get(self.owner, branch=self.branch.pk, product=self.product.pk)
        self.assertNotIn('supplier', response.context['po_form_data'])
        self.assertEqual(len(response.context['po_restore_rows']), 1)  # the product still waits

    def test_manager_cannot_prefill_another_branch(self):
        response = self.get(self.manager, branch=self.other_branch.pk, product=self.product.pk)
        self.assertNotIn('branch', response.context['po_form_data'] or {})
        response = self.get(self.manager, branch=self.branch.pk, product=self.product.pk)
        self.assertEqual(response.context['po_form_data']['branch'], str(self.branch.pk))

    def test_bad_input_is_dropped_never_an_error(self):
        cases = [
            {'product': 'abc'}, {'product': '999999'}, {'product': '²'},
            {'product': str(self.product.pk), 'quantity': '-5'},
            {'product': str(self.product.pk), 'quantity': '0'},
            {'product': str(self.product.pk), 'quantity': 'lots'},
            {'product': str(self.product.pk), 'quantity': '99999999'},
            {'branch': 'xyz'}, {'branch': '99999999'}, {},
        ]
        for params in cases:
            with self.subTest(params=params):
                response = self.get(self.owner, **params)
                self.assertEqual(response.status_code, 200)
                rows = response.context['po_restore_rows']
                if rows:
                    self.assertEqual(rows[0]['product_id'], str(self.product.pk))
                    self.assertEqual(rows[0]['quantity'], 1)  # invalid quantity -> 1

    def test_no_parameters_leaves_the_page_as_it_was(self):
        response = self.get(self.owner)
        self.assertIsNone(response.context['po_form_data'])
        self.assertIsNone(response.context['po_restore_rows'])
        self.assertNotContains(response, 'id="po-restore-rows"')

    def test_staff_are_still_turned_away(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse('procurement_add'), {'product': self.product.pk})
        self.assertEqual(response.status_code, 302)
