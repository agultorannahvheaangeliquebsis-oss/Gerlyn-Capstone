"""P03 - Movement history page, its permissions and branch scoping, the three
_describe_movement additions, and the two closed silent quantity paths
(the warehouse-stock API and the Django admin)."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import BranchManagerProfile, StaffProfile, User
from branches.models import Branch
from core.module_permissions import (
    PRODUCTS, URL_NAME_TO_MODULE, default_module_actions,
)
from inventory.admin import InventoryAdmin, StockMovementAdmin
from inventory.models import (
    Inventory, Product, StockMovement, WarehouseStock,
)
from inventory.views import _describe_movement

MT = StockMovement.MovementType
RT = StockMovement.ReferenceType
URL = 'stock_movement_history'


class _HistoryFixture(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(branch_name='Tarcan', location='Baliuag')
        self.branch_b = Branch.objects.create(branch_name='Makinabang', location='Baliuag')
        self.product = Product.objects.create(
            sku_code='SKU-H1', product_name='Rice', unit_price=Decimal('50.00'))
        self.other_product = Product.objects.create(
            sku_code='SKU-H2', product_name='Sugar', unit_price=Decimal('60.00'))
        self.inv_a = Inventory.objects.create(
            product=self.product, branch=self.branch_a, quantity_on_hand=10)
        self.inv_b = Inventory.objects.create(
            product=self.product, branch=self.branch_b, quantity_on_hand=7)
        self.inv_a2 = Inventory.objects.create(
            product=self.other_product, branch=self.branch_a, quantity_on_hand=3)
        self.ws_a = WarehouseStock.objects.create(
            warehouse=self.branch_a.warehouse, product=self.product,
            quantity_on_hand=40)

        self.owner = User.objects.create_user(
            username='h-owner', password='pw', role=User.Role.OWNER)
        self.manager = User.objects.create_user(
            username='h-manager', password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.filter(user=self.manager).update(branch=self.branch_a)
        self.staff = User.objects.create_user(
            username='h-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.filter(user=self.staff).update(branch=self.branch_a)

    def move(self, inventory=None, warehouse_stock=None, movement_type=MT.SALE,
             change=-1, **extra):
        stock = inventory or warehouse_stock
        return StockMovement.objects.create(
            inventory=inventory, warehouse_stock=warehouse_stock,
            location_type=(StockMovement.LocationType.FLOOR if inventory
                           else StockMovement.LocationType.WAREHOUSE),
            movement_type=movement_type, quantity_change=change,
            quantity_before=stock.quantity_on_hand,
            quantity_after=stock.quantity_on_hand + change, **extra)

    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse(URL), params)

    @staticmethod
    def pks(response):
        return [r['movement'].pk for r in response.context['rows']]


class HistoryPageTests(_HistoryFixture):
    def test_owner_sees_every_branch_newest_first(self):
        a = self.move(self.inv_a)
        b = self.move(self.inv_b)
        w = self.move(warehouse_stock=self.ws_a, movement_type=MT.PROCUREMENT_RECEIVED, change=5)
        response = self.get(self.owner, range='all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pks(response), [w.pk, b.pk, a.pk])

    def test_product_filter_and_panel(self):
        mine = self.move(self.inv_a)
        self.move(self.inv_a2)
        response = self.get(self.owner, product=self.product.pk, range='all')
        self.assertEqual(self.pks(response), [mine.pk])
        panel = response.context['product_panel']
        self.assertEqual(panel['product'], self.product)
        self.assertEqual((panel['floor_on_hand'], panel['warehouse_on_hand']), (17, 40))

    def test_no_panel_without_a_product_filter(self):
        self.assertIsNone(self.get(self.owner).context['product_panel'])

    def test_branch_type_location_and_search_filters(self):
        a = self.move(self.inv_a, movement_type=MT.SALE)
        b = self.move(self.inv_b, movement_type=MT.ADJUSTMENT, notes='recount')
        w = self.move(warehouse_stock=self.ws_a, movement_type=MT.PROCUREMENT_RECEIVED, change=5)
        get = lambda **p: self.pks(self.get(self.owner, range='all', **p))
        self.assertEqual(get(branch=self.branch_b.pk), [b.pk])
        self.assertEqual(get(type=MT.SALE), [a.pk])
        self.assertEqual(get(location='Warehouse'), [w.pk])
        self.assertEqual(get(q='recount'), [b.pk])
        self.assertEqual(get(q='SKU-H1'), [w.pk, b.pk, a.pk])
        self.assertEqual(get(q='Sugar'), [])
        self.assertEqual(get(type='bogus'), [w.pk, b.pk, a.pk])  # ignored, not an error

    def test_date_range_excludes_old_movements(self):
        old = self.move(self.inv_a)
        StockMovement.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=90))
        fresh = self.move(self.inv_a)
        self.assertEqual(self.pks(self.get(self.owner, range='7')), [fresh.pk])
        self.assertEqual(self.pks(self.get(self.owner, range='all')), [fresh.pk, old.pk])

    def test_pagination_is_stable_and_keeps_filters(self):
        from inventory.views import MOVEMENT_HISTORY_PAGE_SIZE as size
        made = [self.move(self.inv_a).pk for _ in range(size + 3)]
        first = self.get(self.owner, range='all', type=MT.SALE)
        self.assertEqual(len(first.context['rows']), size)
        self.assertContains(first, 'type=Sale')  # carried into the Next link
        second = self.get(self.owner, range='all', type=MT.SALE, page=2)
        seen = self.pks(first) + self.pks(second)
        self.assertEqual(sorted(seen), sorted(made))
        self.assertEqual(len(seen), len(set(seen)))

    def test_reference_links_and_labels(self):
        m = self.move(self.inv_a, reference_id=41)
        rows = self.get(self.owner, range='all').context['rows']
        self.assertEqual(rows[0]['reference_label'], 'Sales transaction #41')
        self.assertEqual(rows[0]['reference_url'], reverse('transaction_detail', args=[41]))
        adjustment = self.move(self.inv_a, movement_type=MT.ADJUSTMENT)
        rows = self.get(self.owner, range='all').context['rows']
        self.assertEqual((rows[0]['reference_label'], rows[0]['reference_url']), ('', ''))
        self.assertEqual(rows[1]['movement'].pk, m.pk)

    def test_a_batch_reference_is_labelled_but_not_linked(self):
        self.move(self.inv_a, movement_type=MT.EXPIRY_WRITE_OFF, reference_id=9)
        row = self.get(self.owner, range='all').context['rows'][0]
        self.assertEqual((row['reference_label'], row['reference_url']), ('Stock batch #9', ''))

    def test_movement_survives_its_stock_row_being_deleted(self):
        m = self.move(self.inv_a2)
        self.inv_a2.delete()
        row = self.get(self.owner, range='all').context['rows'][0]
        self.assertEqual(row['movement'].pk, m.pk)
        self.assertEqual(row['product'], 'Sugar')
        self.assertNotIn('record removed', row['product'])

    def test_product_list_links_to_the_history(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('product_list'))
        self.assertContains(response, reverse(URL))
        self.assertContains(response, f'{reverse(URL)}?product={self.product.pk}')


class LargeIdLinkTests(TestCase):
    """USE_THOUSAND_SEPARATOR is on, so an id interpolated raw into a URL or an
    <option value> renders as "1,636" once it reaches four digits - a link
    that points nowhere. Uses explicit large pks so the test does not depend
    on how far the test database's autoincrement happens to have got."""

    def test_ids_of_four_or_more_digits_render_unformatted(self):
        branch = Branch.objects.create(pk=2345, branch_name='Big Branch', location='X')
        product = Product.objects.create(
            pk=1234567, sku_code='SKU-BIG', product_name='Big', unit_price=Decimal('5.00'))
        inv = Inventory.objects.create(product=product, branch=branch, quantity_on_hand=1)
        StockMovement.objects.create(
            inventory=inv, location_type=StockMovement.LocationType.FLOOR,
            movement_type=MT.SALE, quantity_change=-1, quantity_before=2, quantity_after=1)
        owner = User.objects.create_user(
            username='big-owner', password='pw', role=User.Role.OWNER)
        self.client.force_login(owner)

        history = self.client.get(reverse(URL), {'range': 'all'})
        self.assertContains(history, f'{reverse(URL)}?product=1234567"')
        self.assertContains(history, '<option value="2345"')
        self.assertNotContains(history, '1,234,567')
        self.assertNotContains(history, 'value="2,345"')

        products = self.client.get(reverse('product_list'))
        self.assertContains(products, f'{reverse(URL)}?product=1234567"')

        # And the link it renders actually filters to that product.
        filtered = self.client.get(reverse(URL), {'product': '1234567', 'range': 'all'})
        self.assertEqual(len(filtered.context['rows']), 1)
        self.assertEqual(filtered.context['product_panel']['product'], product)


class HistoryPermissionAndScopingTests(_HistoryFixture):
    def test_manager_sees_only_their_branch(self):
        mine = self.move(self.inv_a)
        self.move(self.inv_b)
        response = self.get(self.manager, range='all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pks(response), [mine.pk])
        self.assertEqual(response.context['branches'], [])  # no branch picker

    def test_manager_cannot_widen_scope_with_a_branch_parameter(self):
        mine = self.move(self.inv_a)
        self.move(self.inv_b)
        response = self.get(self.manager, range='all', branch=self.branch_b.pk)
        self.assertEqual(self.pks(response), [mine.pk])

    def test_manager_cannot_reach_another_branch_through_the_product_filter(self):
        self.move(self.inv_b)
        response = self.get(self.manager, range='all', product=self.product.pk)
        self.assertEqual(self.pks(response), [])
        # ...and the panel's on-hand counts only their own branch.
        self.assertEqual(response.context['product_panel']['floor_on_hand'], 10)

    def test_unassigned_manager_sees_nothing(self):
        self.move(self.inv_a)
        BranchManagerProfile.objects.filter(user=self.manager).update(branch=None)
        response = self.get(self.manager, range='all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pks(response), [])

    def test_a_legacy_row_with_no_branch_is_owner_only(self):
        m = self.move(self.inv_a)
        StockMovement.objects.filter(pk=m.pk).update(branch=None)
        self.assertEqual(self.pks(self.get(self.owner, range='all')), [m.pk])
        self.assertEqual(self.pks(self.get(self.manager, range='all')), [])

    def test_default_staff_can_view_only_their_own_branch_history(self):
        # The role matrix gives Staff the Products view by default, and the
        # journal rides on that grant - still scoped to their own branch.
        mine = self.move(self.inv_a)
        self.move(self.inv_b)
        response = self.get(self.staff, range='all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pks(response), [mine.pk])

    def test_staff_without_the_products_grant_is_kept_out(self):
        self.move(self.inv_a)
        self.staff.allowed_modules = []
        self.staff.save(update_fields=['allowed_modules'])
        response = self.get(self.staff, range='all')
        self.assertNotEqual(response.status_code, 200)

    def test_manager_without_the_products_grant_is_kept_out(self):
        self.manager.allowed_modules = []
        self.manager.save(update_fields=['allowed_modules'])
        self.assertNotEqual(self.get(self.manager).status_code, 200)

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse(URL))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_history_is_read_only(self):
        self.client.force_login(self.owner)
        for method in (self.client.post, self.client.put, self.client.delete):
            self.assertEqual(method(reverse(URL)).status_code, 405)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_routes_are_registered_under_the_products_module(self):
        self.assertEqual(URL_NAME_TO_MODULE[URL], PRODUCTS)
        self.assertEqual(URL_NAME_TO_MODULE['api-stock-movements'], PRODUCTS)

    def test_api_movements_now_follow_the_products_grant(self):
        mine = self.move(self.inv_a)
        self.move(self.inv_b)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('api-stock-movements'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r['id'] for r in response.json()], [mine.pk])
        self.manager.allowed_modules = []
        self.manager.save(update_fields=['allowed_modules'])
        self.assertEqual(self.client.get(reverse('api-stock-movements')).status_code, 403)

        # Default Staff have the Products view (role matrix), so the API opens
        # for them too - scoped to their own branch.
        self.client.force_login(self.staff)
        response = self.client.get(reverse('api-stock-movements'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r['id'] for r in response.json()], [mine.pk])
        # ...and it still gates on the grant, not on the Staff role: take the
        # Products module away and the same account is refused.
        self.staff.allowed_modules = []
        self.staff.save(update_fields=['allowed_modules'])
        self.assertEqual(self.client.get(reverse('api-stock-movements')).status_code, 403)


class DescribeMovementTests(_HistoryFixture):
    def describe(self, movement_type, **kw):
        stock = kw.pop('stock', self.inv_a)
        m = self.move(**({'inventory': stock} if stock is self.inv_a
                         else {'warehouse_stock': stock}),
                      movement_type=movement_type)
        return _describe_movement(StockMovement.objects.get(pk=m.pk))

    def test_expiry_write_off_names_the_place_it_left(self):
        self.assertEqual(self.describe(MT.EXPIRY_WRITE_OFF),
                         ('Rice', 'Tarcan Floor', 'Expired — written off'))
        self.assertEqual(self.describe(MT.EXPIRY_WRITE_OFF, stock=self.ws_a),
                         ('Rice', 'Tarcan Warehouse', 'Expired — written off'))

    def test_customer_return(self):
        self.assertEqual(self.describe(MT.CUSTOMER_RETURN),
                         ('Rice', 'Customer refund', 'Tarcan Floor'))

    def test_supplier_return(self):
        self.assertEqual(self.describe(MT.SUPPLIER_RETURN, stock=self.ws_a),
                         ('Rice', 'Tarcan Warehouse', 'Returned to supplier'))

    def test_no_type_falls_through_to_dashes_any_more(self):
        for movement_type in MT.values:
            with self.subTest(movement_type):
                _product, source, destination = self.describe(movement_type)
                self.assertNotEqual((source, destination), ('—', '—'))

    def test_row_whose_stock_row_is_gone_still_describes_itself(self):
        m = self.move(self.inv_a, movement_type=MT.SALE)
        self.inv_a.delete()
        self.assertEqual(_describe_movement(StockMovement.objects.get(pk=m.pk)),
                         ('Rice', 'Tarcan Floor', 'Customer sale'))

    def test_row_with_nothing_left_says_record_removed(self):
        m = self.move(self.inv_a)
        StockMovement.objects.filter(pk=m.pk).update(product=None, branch=None)
        self.inv_a.delete()
        self.assertEqual(_describe_movement(StockMovement.objects.get(pk=m.pk)),
                         ('(record removed)', '—', '—'))


class SilentPathClosureTests(_HistoryFixture):
    def url(self):
        return f'/api/warehouse-stock/{self.ws_a.pk}/'

    def patch_body(self, **fields):
        body = {'reorderLevel': 5}
        body.update(fields)
        return json.dumps(body)

    def test_patch_cannot_change_quantity_on_hand(self):
        self.client.force_login(self.owner)
        original_reorder = self.ws_a.reorder_level
        response = self.client.patch(
            self.url(), data=self.patch_body(quantityOnHand=999),
            content_type='application/json')
        # The whole request is rejected (not silently trimmed), so nothing in
        # it is applied - including the otherwise-editable reorder level.
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('quantityOnHand', response.json())
        self.ws_a.refresh_from_db()
        self.assertEqual(self.ws_a.quantity_on_hand, 40)
        self.assertEqual(self.ws_a.reorder_level, original_reorder)
        self.assertFalse(StockMovement.objects.exists())

        # Sending the current quantity back is not a change, so the editable
        # field still works through the same endpoint.
        response = self.client.patch(
            self.url(), data=self.patch_body(quantityOnHand=40),
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.ws_a.refresh_from_db()
        self.assertEqual(self.ws_a.quantity_on_hand, 40)
        self.assertEqual(self.ws_a.reorder_level, 5)
        self.assertEqual(response.json()['quantityOnHand'], 40)
        self.assertFalse(StockMovement.objects.exists())

    def test_put_cannot_change_quantity_on_hand(self):
        self.client.force_login(self.owner)
        response = self.client.put(
            self.url(), data=self.patch_body(quantityOnHand=0),
            content_type='application/json')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('quantityOnHand', response.json())
        self.ws_a.refresh_from_db()
        self.assertEqual(self.ws_a.quantity_on_hand, 40)
        self.assertFalse(StockMovement.objects.exists())

    def test_serializer_marks_quantity_read_only(self):
        from inventory.serializers import WarehouseStockSerializer
        self.assertTrue(WarehouseStockSerializer().fields['quantityOnHand'].read_only)

    def test_inventory_admin_makes_the_counters_read_only(self):
        model_admin = InventoryAdmin(Inventory, admin.site)
        request = RequestFactory().get('/')
        request.user = self.owner
        readonly = model_admin.get_readonly_fields(request, self.inv_a)
        self.assertIn('quantity_on_hand', readonly)
        self.assertNotIn('quantity_on_hand', model_admin.get_form(request, self.inv_a).base_fields)
        self.assertIn('reorder_level', model_admin.get_form(request, self.inv_a).base_fields)

    def test_stock_movement_admin_is_fully_read_only(self):
        model_admin = StockMovementAdmin(StockMovement, admin.site)
        request = RequestFactory().get('/')
        request.user = User.objects.create_superuser(
            username='root', password='pw', email='root@example.com')
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        editable = {f.name for f in StockMovement._meta.get_fields()
                    if getattr(f, 'editable', False) and not f.auto_created}
        self.assertLessEqual(editable, set(model_admin.readonly_fields))
