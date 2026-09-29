import io
from datetime import date
from decimal import Decimal
from io import StringIO

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, User
from branches.models import Branch
from core.module_permissions import (
    ADD, DELETE, EDIT, MODULE_PERMISSION_REGISTRY, PRODUCTS, VIEW,
    default_module_actions, user_can_perform_module_action,
)
from inventory.alerts import batch_inventory_mismatches
from inventory.models import (
    Batch, Inventory, Product, ProductCategory, ProductImportMappingTemplate,
    Warehouse, WarehouseStock,
)


class CategoryModalFlowTests(TestCase):
    """Add/Edit Category moved from their own pages into a modal on the
    category list. These cover the parts of that move a template cannot show:
    that the old URLs still land somewhere sensible, and that a rejected
    submit comes back with the modal open and the typed values intact."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def test_get_add_url_redirects_to_list_with_open_flag(self):
        """A bookmark of the old Add Category page opens the modal instead."""
        response = self.client.get(reverse('category_add'))
        self.assertRedirects(response, reverse('category_list') + '?add=1')

    def test_get_edit_url_redirects_to_list_with_pk(self):
        category = ProductCategory.objects.create(category_name='Canned Goods')
        response = self.client.get(reverse('category_edit', args=[category.pk]))
        self.assertRedirects(
            response, reverse('category_list') + f'?edit={category.pk}'
        )

    def test_post_creates_category_and_returns_to_list(self):
        response = self.client.post(
            reverse('category_add'),
            {'category_name': 'Canned Goods', 'description': 'Tinned food'},
        )
        self.assertRedirects(response, reverse('category_list'))
        category = ProductCategory.objects.get(category_name='Canned Goods')
        self.assertEqual(category.description, 'Tinned food')

    def test_blank_name_reopens_modal_and_keeps_description(self):
        response = self.client.post(
            reverse('category_add'),
            {'category_name': '', 'description': 'Tinned food'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertIn('Category name is required.', response.context['add_errors'])
        # The typed description survives the round trip — a redirect would
        # have discarded it.
        self.assertContains(response, 'Tinned food')
        self.assertFalse(ProductCategory.objects.exists())

    def test_duplicate_name_is_rejected_with_the_modal_open(self):
        ProductCategory.objects.create(category_name='Canned Goods')
        response = self.client.post(
            reverse('category_add'), {'category_name': 'canned goods'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_add_modal'])
        self.assertEqual(ProductCategory.objects.count(), 1)

    def test_edit_saves_and_a_clash_reopens_the_edit_modal(self):
        first = ProductCategory.objects.create(category_name='Canned Goods')
        second = ProductCategory.objects.create(category_name='Snacks')

        ok = self.client.post(
            reverse('category_edit', args=[second.pk]),
            {'category_name': 'Beverages', 'description': ''},
        )
        self.assertRedirects(ok, reverse('category_list'))
        second.refresh_from_db()
        self.assertEqual(second.category_name, 'Beverages')

        clash = self.client.post(
            reverse('category_edit', args=[second.pk]),
            {'category_name': 'Canned Goods'},
        )
        self.assertEqual(clash.status_code, 200)
        self.assertTrue(clash.context['show_edit_modal'])
        self.assertEqual(clash.context['edit_category'].pk, second.pk)
        second.refresh_from_db()
        self.assertEqual(second.category_name, 'Beverages')
        # Renaming to its own current name is not a clash with itself.
        same = self.client.post(
            reverse('category_edit', args=[first.pk]),
            {'category_name': 'Canned Goods', 'description': 'Tinned'},
        )
        self.assertRedirects(same, reverse('category_list'))

    def test_list_opens_the_edit_modal_for_a_valid_edit_param(self):
        category = ProductCategory.objects.create(category_name='Canned Goods')
        response = self.client.get(
            reverse('category_list'), {'edit': category.pk}
        )
        self.assertTrue(response.context['show_edit_modal'])
        self.assertEqual(response.context['edit_category'].pk, category.pk)

    def test_list_ignores_an_unknown_edit_param(self):
        response = self.client.get(reverse('category_list'), {'edit': '9999'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('show_edit_modal', response.context)

    def test_product_list_still_renders_after_the_modal_move(self):
        """The modal CSS and initModal() moved out of product_list.html into
        gerlyn.css and static/js/modal.js. Nothing about the page should have
        changed, so this is here to catch it if the extraction broke it.

        Add/Edit Product later moved off this page entirely, onto their own
        product_form.html — so what survives here is the link to it, not a
        modal id."""
        response = self.client.get(reverse('product_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('product_add'))

    def test_staff_cannot_post_a_category(self):
        staff = User.objects.create_user(
            username='staff', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(staff)
        response = self.client.post(
            reverse('category_add'), {'category_name': 'Canned Goods'}
        )
        # Bounced back to the list, and nothing created. Not following the
        # redirect: since F-07 a new STAFF has no Inventory module, so the
        # list GET itself redirects — which is not what this test is about.
        self.assertRedirects(
            response, reverse('accounts:staff_dashboard'),
            fetch_redirect_response=False,
        )
        self.assertFalse(
            ProductCategory.objects.filter(category_name='Canned Goods').exists()
        )


class _ManagerGrantTestBase(TestCase):
    """Shared setup for the "Managers are view-only by default, but an Owner
    can grant Add/Edit/Delete per account" tests (Product Master List module:
    products, categories and the bulk import all sit under it).

    Nothing here reimplements the grant system - the Manager is a normal
    account whose `module_actions` is either the default a new account gets
    (no Add/Edit/Delete on products) or an explicit grant, exactly what the
    Permission Matrix writes."""

    def setUp(self):
        self.branch = Branch.objects.create(branch_name='Baliuag Main', location='Baliuag')
        self.owner = User.objects.create_user(
            username='grant-owner', password='pw', role=User.Role.OWNER
        )
        # A brand-new Manager: default_module_actions() is what it is created with.
        self.manager = User.objects.create_user(
            username='grant-manager', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        # A signal already made the manager's profile; just point it at a branch.
        BranchManagerProfile.objects.filter(user=self.manager).update(
            branch=self.branch, manager_name='Grant Manager',
        )
        self.category = ProductCategory.objects.create(category_name='Canned Goods')
        self.product = Product.objects.create(
            sku_code='SKU-GRANT', product_name='Existing Product',
            unit_of_measure=Product.UnitOfMeasure.PIECE, unit_price=10,
            category=self.category,
        )

    def grant(self, *actions):
        """What the Owner does in the Permission Matrix: View on Product Master
        List plus the given write actions."""
        self.manager.allowed_modules = [PRODUCTS]
        module_actions = default_module_actions()
        module_actions[PRODUCTS] = list(actions)
        self.manager.module_actions = module_actions
        self.manager.save(update_fields=['allowed_modules', 'module_actions'])

    def product_payload(self, name):
        return {
            'product_name': name,
            'unit_of_measure': Product.UnitOfMeasure.PIECE,
            'unit_price': '10.00',
        }


class DefaultManagerProductPermissionTests(_ManagerGrantTestBase):
    """A new Manager can view products and categories but not change them."""

    def test_new_accounts_get_no_product_write_actions_by_default(self):
        self.assertEqual(default_module_actions()[PRODUCTS], [])
        self.assertTrue(user_can_perform_module_action(self.manager, PRODUCTS, VIEW))
        for action in (ADD, EDIT, DELETE):
            self.assertFalse(
                user_can_perform_module_action(self.manager, PRODUCTS, action)
            )

    def test_the_default_only_withholds_the_product_module(self):
        """Every other module still starts with all of its write actions."""
        defaults = default_module_actions()
        for code, actions in defaults.items():
            if code != PRODUCTS:
                self.assertEqual(
                    actions,
                    [a for a in (ADD, EDIT, DELETE)
                     if MODULE_PERMISSION_REGISTRY[code][a]],
                    code,
                )

    # ---- products -------------------------------------------------------
    def test_default_manager_can_view_products_but_sees_no_write_controls(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Existing Product')
        self.assertFalse(response.context['can_add'])
        self.assertFalse(response.context['can_edit'])
        self.assertFalse(response.context['can_delete'])
        self.assertNotContains(response, reverse('product_add'))
        self.assertNotContains(response, reverse('product_import_upload'))
        self.assertNotContains(
            response, reverse('product_edit', args=[self.product.pk])
        )
        self.assertNotContains(
            response, reverse('product_delete', args=[self.product.pk])
        )

    def test_default_manager_cannot_add_edit_or_delete_a_product(self):
        self.client.force_login(self.manager)

        self.client.post(reverse('product_add'), self.product_payload('Blocked Add'))
        self.assertFalse(Product.objects.filter(product_name='Blocked Add').exists())

        self.client.post(
            reverse('product_edit', args=[self.product.pk]),
            self.product_payload('Renamed'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.product_name, 'Existing Product')

        self.client.post(reverse('product_delete', args=[self.product.pk]))
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_default_manager_cannot_write_products_through_the_api(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('api-product-list')).status_code, 200)
        detail = reverse('api-product-detail', args=[self.product.pk])

        created = self.client.post(
            reverse('api-product-list'),
            {'sku': 'SKU-API', 'name': 'Blocked API', 'unit': 'Piece', 'retailPrice': '10.00'},
        )
        updated = self.client.patch(
            detail, {'name': 'Renamed'}, content_type='application/json'
        )
        deleted = self.client.delete(detail)

        self.assertEqual(created.status_code, 403)
        self.assertEqual(updated.status_code, 403)
        self.assertEqual(deleted.status_code, 403)
        self.assertFalse(Product.objects.filter(product_name='Blocked API').exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.product_name, 'Existing Product')

    # ---- categories -------------------------------------------------------
    def test_default_manager_can_view_categories_but_sees_no_write_controls(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('category_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Canned Goods')
        self.assertFalse(response.context['can_add'])
        self.assertFalse(response.context['can_edit'])
        self.assertFalse(response.context['can_delete'])
        self.assertNotContains(
            response, reverse('category_delete', args=[self.category.pk])
        )
        self.assertNotContains(
            response, reverse('category_edit', args=[self.category.pk])
        )

    def test_default_manager_cannot_open_the_category_edit_modal_by_url(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse('category_list'), {'edit': self.category.pk}
        )
        self.assertNotIn('show_edit_modal', response.context)

    def test_default_manager_cannot_add_edit_or_delete_a_category(self):
        self.client.force_login(self.manager)

        self.client.post(reverse('category_add'), {'category_name': 'Beverages'})
        self.assertFalse(
            ProductCategory.objects.filter(category_name='Beverages').exists()
        )

        self.client.post(
            reverse('category_edit', args=[self.category.pk]),
            {'category_name': 'Renamed'},
        )
        self.category.refresh_from_db()
        self.assertEqual(self.category.category_name, 'Canned Goods')

        self.client.post(reverse('category_delete', args=[self.category.pk]))
        self.assertTrue(ProductCategory.objects.filter(pk=self.category.pk).exists())

    def test_default_manager_cannot_write_categories_through_the_api(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('api-category-list')).status_code, 200)
        detail = reverse('api-category-detail', args=[self.category.pk])

        created = self.client.post(
            reverse('api-category-list'), {'categoryName': 'Beverages'}
        )
        updated = self.client.patch(
            detail, {'categoryName': 'Renamed'}, content_type='application/json'
        )
        deleted = self.client.delete(detail)

        self.assertEqual(created.status_code, 403)
        self.assertEqual(updated.status_code, 403)
        self.assertEqual(deleted.status_code, 403)
        self.assertFalse(
            ProductCategory.objects.filter(category_name='Beverages').exists()
        )
        self.category.refresh_from_db()
        self.assertEqual(self.category.category_name, 'Canned Goods')

    # ---- bulk import (same permission as Add) -----------------------------
    def test_default_manager_cannot_start_a_bulk_import(self):
        """The import creates products (and categories), so it needs Add."""
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_import_upload'))
        self.assertNotEqual(
            response.url, f"{reverse('product_list')}?import=upload"
        )


class GrantedManagerProductPermissionTests(_ManagerGrantTestBase):
    """An Owner can still grant a specific Manager product/category access
    through the Permission Matrix - the default is not an absolute block."""

    def test_granted_manager_can_add_edit_and_delete_products(self):
        self.grant(ADD, EDIT, DELETE)
        self.client.force_login(self.manager)

        self.client.post(reverse('product_add'), self.product_payload('Granted Add'))
        created = Product.objects.get(product_name='Granted Add')

        self.client.post(
            reverse('product_edit', args=[created.pk]),
            self.product_payload('Granted Edit'),
        )
        created.refresh_from_db()
        self.assertEqual(created.product_name, 'Granted Edit')

        self.client.post(reverse('product_delete', args=[created.pk]))
        self.assertFalse(Product.objects.filter(pk=created.pk).exists())

    def test_granted_manager_can_add_edit_and_delete_categories(self):
        self.grant(ADD, EDIT, DELETE)
        self.client.force_login(self.manager)

        self.client.post(reverse('category_add'), {'category_name': 'Beverages'})
        added = ProductCategory.objects.get(category_name='Beverages')

        self.client.post(
            reverse('category_edit', args=[added.pk]), {'category_name': 'Drinks'}
        )
        added.refresh_from_db()
        self.assertEqual(added.category_name, 'Drinks')

        self.client.post(reverse('category_delete', args=[added.pk]))
        self.assertFalse(ProductCategory.objects.filter(pk=added.pk).exists())

    def test_granted_manager_sees_the_matching_controls(self):
        self.grant(ADD, EDIT, DELETE)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_list'))
        self.assertTrue(response.context['can_add'])
        self.assertTrue(response.context['can_edit'])
        self.assertTrue(response.context['can_delete'])
        self.assertContains(response, reverse('product_import_upload'))

    def test_a_single_grant_only_opens_that_action(self):
        self.grant(ADD)
        self.client.force_login(self.manager)

        self.client.post(reverse('category_add'), {'category_name': 'Beverages'})
        self.assertTrue(
            ProductCategory.objects.filter(category_name='Beverages').exists()
        )
        self.client.post(
            reverse('category_edit', args=[self.category.pk]),
            {'category_name': 'Renamed'},
        )
        self.category.refresh_from_db()
        self.assertEqual(self.category.category_name, 'Canned Goods')
        self.client.post(reverse('category_delete', args=[self.category.pk]))
        self.assertTrue(ProductCategory.objects.filter(pk=self.category.pk).exists())

    def test_granted_manager_can_start_a_bulk_import(self):
        self.grant(ADD)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_import_upload'))
        self.assertRedirects(
            response, f"{reverse('product_list')}?import=upload",
            fetch_redirect_response=False,
        )

    def test_granted_manager_can_write_through_the_api(self):
        self.grant(ADD, EDIT, DELETE)
        self.client.force_login(self.manager)
        created = self.client.post(
            reverse('api-category-list'), {'categoryName': 'Beverages'}
        )
        self.assertEqual(created.status_code, 201)
        updated = self.client.patch(
            reverse('api-product-detail', args=[self.product.pk]),
            {'name': 'API Renamed'}, content_type='application/json',
        )
        self.assertEqual(updated.status_code, 200)


class OwnerProductPermissionTests(_ManagerGrantTestBase):
    """The Owner is exempt from the grant layer and keeps full access."""

    def test_owner_sees_all_controls_and_can_write_everything(self):
        self.client.force_login(self.owner)
        listing = self.client.get(reverse('product_list'))
        self.assertTrue(listing.context['can_add'])
        self.assertTrue(listing.context['can_edit'])
        self.assertTrue(listing.context['can_delete'])
        self.assertContains(listing, reverse('product_import_upload'))

        self.client.post(reverse('product_add'), self.product_payload('Owner Added'))
        created = Product.objects.get(product_name='Owner Added')
        self.client.post(
            reverse('product_edit', args=[created.pk]),
            self.product_payload('Owner Edited'),
        )
        created.refresh_from_db()
        self.assertEqual(created.product_name, 'Owner Edited')
        self.client.post(reverse('product_delete', args=[created.pk]))
        self.assertFalse(Product.objects.filter(pk=created.pk).exists())

        self.client.post(reverse('category_add'), {'category_name': 'Beverages'})
        self.assertTrue(
            ProductCategory.objects.filter(category_name='Beverages').exists()
        )

    def test_owner_can_write_through_the_api(self):
        self.client.force_login(self.owner)
        created = self.client.post(
            reverse('api-category-list'), {'categoryName': 'Beverages'}
        )
        self.assertEqual(created.status_code, 201)
        updated = self.client.patch(
            reverse('api-product-detail', args=[self.product.pk]),
            {'name': 'Owner API Rename'}, content_type='application/json',
        )
        self.assertEqual(updated.status_code, 200)

    def test_staff_are_still_blocked_from_deleting_products(self):
        staff = User.objects.create_user(
            username='grant-staff', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(staff)
        self.client.post(reverse('product_delete', args=[self.product.pk]))
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())


class SeedCategoriesCommandTests(TestCase):
    """seed_categories runs against a live catalog, unlike seed_demo which
    wipes one first — so what matters is that it adds without destroying and
    that a second run is a no-op."""

    def test_it_creates_the_expected_categories_flat(self):
        call_command('seed_categories', stdout=StringIO())

        self.assertTrue(ProductCategory.objects.filter(category_name='Canned Goods').exists())
        self.assertTrue(ProductCategory.objects.filter(category_name='Sardines & Fish').exists())

    def test_running_it_twice_creates_nothing_new(self):
        call_command('seed_categories', stdout=StringIO())
        before = ProductCategory.objects.count()

        call_command('seed_categories', stdout=StringIO())

        self.assertEqual(ProductCategory.objects.count(), before)

    def test_dry_run_writes_nothing(self):
        call_command('seed_categories', '--dry-run', stdout=StringIO())

        self.assertEqual(ProductCategory.objects.count(), 0)

    def test_it_adopts_a_category_the_owner_already_typed_in(self):
        """Same name, no duplicate row — it is adopted, not cloned."""
        ProductCategory.objects.create(category_name='Biscuits')

        call_command('seed_categories', stdout=StringIO())

        self.assertEqual(
            ProductCategory.objects.filter(category_name='Biscuits').count(), 1
        )

    def test_it_deletes_no_existing_category(self):
        general = ProductCategory.objects.create(category_name='General')

        call_command('seed_categories', stdout=StringIO())

        self.assertTrue(ProductCategory.objects.filter(pk=general.pk).exists())

    def test_it_files_a_known_product_onto_its_shelf(self):
        product = Product.objects.create(
            sku_code='DEMO-003', product_name='Canned Sardines',
            unit_of_measure='Can', unit_price=Decimal('35.00'),
        )

        call_command('seed_categories', stdout=StringIO())

        product.refresh_from_db()
        self.assertEqual(product.category.category_name, 'Sardines & Fish')

    def test_it_does_not_refile_an_unlisted_product(self):
        mine = ProductCategory.objects.create(category_name='My Shelf')
        product = Product.objects.create(
            sku_code='SKU-MINE', product_name='Something', category=mine,
            unit_of_measure='Piece', unit_price=Decimal('10.00'),
        )

        call_command('seed_categories', stdout=StringIO())

        product.refresh_from_db()
        self.assertEqual(product.category, mine)


def _xlsx_upload(filename, header, data_rows):
    """Build an in-memory .xlsx matching what a real browser upload of the
    given header/rows would produce, for POSTing through product_import_upload."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for row in data_rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return SimpleUploadedFile(
        filename, buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


class ProductImportExportTests(TestCase):
    """The Excel import/export wizard for the Product Master List: export
    downloads the catalog, import is upload -> map columns -> preview ->
    confirm, matching existing products by barcode."""

    # 'Bulk Qty 1' is blank in every row these tests upload, so none of them
    # carries a ladder; the rung columns get their own test below.
    HEADER = [
        'Item Name', 'Barcode', 'Cat', 'UOM', 'SRP',
        'Bulk Qty 1', 'Notes', 'VAT', 'PWD', 'Senior',
    ]
    MAPPING = {
        'map_product_name': 'Item Name',
        'map_barcode': 'Barcode',
        'map_category': 'Cat',
        'map_unit_of_measure': 'UOM',
        'map_unit_price': 'SRP',
        'map_tier1_min_quantity': 'Bulk Qty 1',
        'map_description': 'Notes',
        'map_is_vat_exempt': 'VAT',
        'map_is_pwd_eligible': 'PWD',
        'map_is_senior_eligible': 'Senior',
    }

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def _upload(self, data_rows, filename='import.xlsx'):
        return self.client.post(
            reverse('product_import_upload'),
            {'file': _xlsx_upload(filename, self.HEADER, data_rows)},
        )

    # ── Export ──────────────────────────────────────────────

    def test_export_returns_an_xlsx_with_a_row_per_product(self):
        category = ProductCategory.objects.create(category_name='Snacks')
        product = Product.objects.create(
            sku_code='SKU-EXPORT-1', product_name='Chippy', barcode='EXPORT001',
            category=category, unit_of_measure=Product.UnitOfMeasure.PIECE,
            unit_price=15,
        )
        product.price_tiers.create(min_quantity=24, unit_price=12)
        response = self.client.get(reverse('product_export_xlsx'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(rows[0][0], 'SKU Code')
        data_row = next(r for r in rows[1:] if r[1] == 'Chippy')
        self.assertEqual(data_row[2], 'EXPORT001')
        self.assertEqual(data_row[3], 'Snacks')
        # The ladder exports into the fixed rung columns; unused rungs are blank,
        # so every row still lines up under the same headers.
        headers = list(rows[0])
        self.assertEqual(data_row[headers.index('Bulk Qty 1')], 24)
        self.assertEqual(data_row[headers.index('Bulk Price 1')], 12)
        self.assertIn(data_row[headers.index('Bulk Qty 2')], (None, ''))

    def test_export_denied_for_staff(self):
        staff = User.objects.create_user(
            username='staff', password='pw', role=User.Role.STAFF
        )
        self.client.force_login(staff)
        response = self.client.get(reverse('product_export_xlsx'))
        self.assertRedirects(
            response, reverse('accounts:staff_dashboard'),
            fetch_redirect_response=False,
        )

    # ── Upload ──────────────────────────────────────────────

    def test_upload_rejects_a_non_xlsx_file(self):
        bad = SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain')
        response = self.client.post(reverse('product_import_upload'), {'file': bad})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose a .xlsx file')

    def test_upload_then_map_guesses_columns_from_headers(self):
        response = self._upload([['Widget', '', '', '', '10', '', '', '', '', '']])
        self.assertRedirects(response, f"{reverse('product_list')}?import=map")
        map_response = self.client.get(response.url)
        fields_by_key = {f['key']: f for f in map_response.context['import_fields']}
        self.assertEqual(fields_by_key['product_name']['selected'], 'Item Name')
        self.assertEqual(fields_by_key['unit_price']['selected'], 'SRP')

    # ── Full wizard: preview + commit ──────────────────────

    def test_full_wizard_creates_updates_and_skips_bad_rows(self):
        existing = Product.objects.create(
            sku_code='SKU-EXISTING-1', product_name='Old Name', barcode='EXISTBAR',
            unit_of_measure=Product.UnitOfMeasure.PIECE, unit_price=50,
        )
        rows = [
            ['New Product A', 'NEWBAR001', 'New Cat X', 'Piece', '50', '', 'desc', 'No', 'Yes', ''],
            ['Updated Name', 'EXISTBAR', '', '', '75', '', '', '', '', ''],
            ['', '', '', '', '', '', '', '', '', ''],  # blank row — silently skipped
            ['Missing Price', 'BADPRICE1', '', '', '', '', '', '', '', ''],
            ['Dup First', 'DUPBAR', '', '', '10', '', '', '', '', ''],
            ['Dup Second', 'DUPBAR', '', '', '20', '', '', '', '', ''],
            ['Bad Unit', 'BADUOM1', '', 'bogus-unit', '30', '', '', '', '', ''],
        ]
        self._upload(rows)
        self.client.post(reverse('product_import_map'), self.MAPPING)

        preview = self.client.get(reverse('product_import_preview'), follow=True)
        self.assertEqual(preview.context['import_counts'], {
            'total': 6, 'create': 2, 'update': 1, 'error': 3,
        })
        created_action = next(r for r in preview.context['import_rows'] if r['barcode'] == 'NEWBAR001')
        self.assertEqual(created_action['action'], 'create')

        commit = self.client.post(reverse('product_import_preview'))
        self.assertRedirects(commit, reverse('product_list'))

        created = Product.objects.get(barcode='NEWBAR001')
        self.assertEqual(created.category.category_name, 'New Cat X')
        self.assertTrue(ProductCategory.objects.filter(category_name='New Cat X').exists())

        existing.refresh_from_db()
        self.assertEqual(existing.product_name, 'Updated Name')
        self.assertEqual(existing.unit_price, 75)

        self.assertEqual(Product.objects.filter(barcode='DUPBAR').count(), 1)
        self.assertEqual(Product.objects.get(barcode='DUPBAR').unit_price, 10)

        self.assertFalse(Product.objects.filter(barcode='BADPRICE1').exists())
        self.assertFalse(Product.objects.filter(barcode='BADUOM1').exists())

    def test_add_only_import_creates_new_rows_but_cannot_update_existing(self):
        existing = Product.objects.create(
            sku_code='SKU-IMPORT-LOCKED', product_name='Keep This Name',
            barcode='IMPORTLOCKED', unit_of_measure=Product.UnitOfMeasure.PIECE,
            unit_price=50,
        )
        manager = User.objects.create_user(
            username='add-only-importer', password='pw',
            role=User.Role.BRANCH_MANAGER,
        )
        manager.allowed_modules = [PRODUCTS]
        manager.module_actions = {
            code: ([ADD] if code == PRODUCTS else [])
            for code in default_module_actions()
        }
        manager.save(update_fields=['allowed_modules', 'module_actions'])
        self.client.force_login(manager)

        self._upload([
            ['Blocked Rename', 'IMPORTLOCKED', '', 'Piece', '75', '', '', '', '', ''],
            ['Allowed New Product', 'IMPORTNEW', '', 'Piece', '25', '', '', '', '', ''],
        ])
        self.client.post(reverse('product_import_map'), self.MAPPING)

        preview = self.client.get(reverse('product_import_preview'), follow=True)
        self.assertEqual(preview.context['import_counts'], {
            'total': 2, 'create': 1, 'update': 0, 'error': 1,
        })
        self.assertIn(
            'Edit permission is required',
            next(
                row for row in preview.context['import_rows']
                if row['barcode'] == 'IMPORTLOCKED'
            )['errors'][0],
        )

        self.client.post(reverse('product_import_preview'))
        existing.refresh_from_db()
        self.assertEqual(existing.product_name, 'Keep This Name')
        self.assertTrue(Product.objects.filter(barcode='IMPORTNEW').exists())

    def test_import_writes_a_bulk_ladder_and_refuses_a_bad_one(self):
        """The rung column-pairs are held to the same rules as the product
        form. A row with no rungs updates a product without wiping the ladder
        it already has — blank bulk columns are not an instruction to clear."""
        keeper = Product.objects.create(
            sku_code='SKU-KEEP-1', product_name='Keeper', barcode='KEEPTIERS',
            unit_of_measure=Product.UnitOfMeasure.PIECE, unit_price=50,
        )
        keeper.price_tiers.create(min_quantity=10, unit_price=45)

        header = [
            'Item Name', 'Barcode', 'SRP',
            'Bulk Qty 1', 'Bulk Price 1', 'Bulk Qty 2', 'Bulk Price 2',
        ]
        rows = [
            ['Laddered', 'LADDER1', '50', '12', '45', '24', '40'],
            ['Above Retail', 'ABOVE1', '50', '12', '55', '', ''],
            ['Rising', 'RISING1', '50', '12', '40', '24', '45'],
            ['Half Row', 'HALF1', '50', '12', '', '', ''],
            ['Keeper Renamed', 'KEEPTIERS', '60', '', '', '', ''],
        ]
        self.client.post(
            reverse('product_import_upload'),
            {'file': _xlsx_upload('tiers.xlsx', header, rows)},
        )
        self.client.post(reverse('product_import_map'), {
            'map_product_name': 'Item Name',
            'map_barcode': 'Barcode',
            'map_unit_price': 'SRP',
            'map_tier1_min_quantity': 'Bulk Qty 1',
            'map_tier1_price': 'Bulk Price 1',
            'map_tier2_min_quantity': 'Bulk Qty 2',
            'map_tier2_price': 'Bulk Price 2',
        })

        preview = self.client.get(reverse('product_import_preview'), follow=True)
        self.assertEqual(preview.context['import_counts'], {
            'total': 5, 'create': 1, 'update': 1, 'error': 3,
        })

        self.client.post(reverse('product_import_preview'))

        laddered = Product.objects.get(barcode='LADDER1')
        self.assertEqual(
            list(laddered.price_tiers.values_list('min_quantity', 'unit_price')),
            [(12, 45), (24, 40)],
        )
        for barcode in ('ABOVE1', 'RISING1', 'HALF1'):
            self.assertFalse(Product.objects.filter(barcode=barcode).exists(), barcode)

        keeper.refresh_from_db()
        self.assertEqual(keeper.unit_price, 60)
        self.assertEqual(
            list(keeper.price_tiers.values_list('min_quantity', 'unit_price')),
            [(10, 45)],
        )

    # ── Saved mapping templates ─────────────────────────────

    def test_import_normalizes_optional_numbers_prices_and_barcodes(self):
        """Blank optional tiers stay valid and populated Excel values are
        normalized identically for preview and commit."""
        header = [
            'Item Name', 'Barcode', 'SRP',
            'Bulk Qty 1', 'Bulk Price 1', 'Bulk Qty 2', 'Bulk Price 2',
            'Bulk Qty 3', 'Bulk Price 3',
        ]
        rows = [
            [
                'Coffee Mate 5g x 48s', 4800361000826.0, 95.00,
                None, None, '', '', '   ', '   ',
            ],
            ['Numeric Pair', 'NUMERICPAIR', 100, 12, 90, None, None, None, None],
            ['String Pair', 'STRINGPAIR', '₱1,095.00', '12', '90.00', '', '', '', ''],
            ['Leading Zero', '00123.0', '95.00', '', '', None, None, '', ''],
        ]
        self.client.post(
            reverse('product_import_upload'),
            {'file': _xlsx_upload('normalized.xlsx', header, rows)},
        )
        self.client.post(reverse('product_import_map'), {
            'map_product_name': 'Item Name',
            'map_barcode': 'Barcode',
            'map_unit_price': 'SRP',
            'map_tier1_min_quantity': 'Bulk Qty 1',
            'map_tier1_price': 'Bulk Price 1',
            'map_tier2_min_quantity': 'Bulk Qty 2',
            'map_tier2_price': 'Bulk Price 2',
            'map_tier3_min_quantity': 'Bulk Qty 3',
            'map_tier3_price': 'Bulk Price 3',
        })

        preview = self.client.get(reverse('product_import_preview'), follow=True)
        self.assertEqual(preview.context['import_counts'], {
            'total': 4, 'create': 4, 'update': 0, 'error': 0,
        })
        preview_rows = {
            row['product_name']: row for row in preview.context['import_rows']
        }
        coffee = preview_rows['Coffee Mate 5g x 48s']
        self.assertEqual(coffee['barcode'], '4800361000826')
        self.assertEqual(coffee['unit_price'], Decimal('95.00'))
        self.assertEqual(coffee['price_tiers'], [])
        self.assertEqual(
            preview_rows['Numeric Pair']['price_tiers'],
            [(12, Decimal('90'))],
        )
        self.assertEqual(
            preview_rows['String Pair']['price_tiers'],
            [(12, Decimal('90.00'))],
        )
        self.assertEqual(preview_rows['Leading Zero']['barcode'], '00123')

        self.client.post(reverse('product_import_preview'))
        coffee = Product.objects.get(barcode='4800361000826')
        self.assertEqual(coffee.unit_price, Decimal('95.00'))
        self.assertFalse(coffee.price_tiers.exists())
        self.assertTrue(Product.objects.filter(barcode='00123').exists())
        self.assertEqual(
            list(
                Product.objects.get(barcode='STRINGPAIR').price_tiers.values_list(
                    'min_quantity', 'unit_price'
                )
            ),
            [(12, Decimal('90.00'))],
        )

    def test_import_reports_the_exact_bulk_field_error(self):
        """Invalid and half-filled pairs identify the field that needs work."""
        header = [
            'Item Name', 'Barcode', 'SRP', 'Bulk Qty 1', 'Bulk Price 1',
        ]
        rows = [
            ['Bad Qty', 'BADQTY', 100, 'abc', 90],
            ['Bad Price', 'BADPRICE', 100, 12, 'abc'],
            ['Missing Price', 'MISSPRICE', 100, 12, '   '],
            ['Missing Qty', 'MISSQTY', 100, '', 90],
            ['', 'NONAME', 100, '', ''],
            ['No Retail', 'NOPRICE', ' ', '', ''],
        ]
        self.client.post(
            reverse('product_import_upload'),
            {'file': _xlsx_upload('invalid-numbers.xlsx', header, rows)},
        )
        self.client.post(reverse('product_import_map'), {
            'map_product_name': 'Item Name',
            'map_barcode': 'Barcode',
            'map_unit_price': 'SRP',
            'map_tier1_min_quantity': 'Bulk Qty 1',
            'map_tier1_price': 'Bulk Price 1',
        })

        preview = self.client.get(reverse('product_import_preview'), follow=True)
        self.assertEqual(preview.context['import_counts']['error'], 6)
        preview_rows = {row['barcode']: row for row in preview.context['import_rows']}
        self.assertEqual(preview_rows['BADQTY']['errors'], [
            'Bulk Qty 1 must be a number.',
        ])
        self.assertEqual(preview_rows['BADPRICE']['errors'], [
            'Bulk Price 1 must be a number.',
        ])
        self.assertEqual(preview_rows['MISSPRICE']['errors'], [
            'Bulk Price 1 is required when Bulk Qty 1 is provided.',
        ])
        self.assertEqual(preview_rows['MISSQTY']['errors'], [
            'Bulk Qty 1 is required when Bulk Price 1 is provided.',
        ])
        self.assertIn('Product Name is required.', preview_rows['NONAME']['errors'])
        self.assertIn('Retail Price is required.', preview_rows['NOPRICE']['errors'])

        self.client.post(reverse('product_import_preview'))
        self.assertFalse(Product.objects.filter(
            barcode__in=['BADQTY', 'BADPRICE', 'MISSPRICE', 'MISSQTY', 'NONAME', 'NOPRICE']
        ).exists())

    def test_saved_private_template_is_hidden_from_other_users(self):
        self._upload([['Widget', '', '', '', '10', '', '', '', '', '']])
        payload = dict(self.MAPPING, save_template_name='My Private Mapping',
                       template_visibility='private')
        self.client.post(reverse('product_import_map'), payload)
        self.assertTrue(
            ProductImportMappingTemplate.objects.filter(
                name='My Private Mapping', created_by=self.owner, is_shared=False,
            ).exists()
        )

        other = User.objects.create_user(
            username='manager2', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(other)
        self._upload([['Widget', '', '', '', '10', '', '', '', '', '']])
        map_response = self.client.get(reverse('product_import_map'), follow=True)
        names = [t.name for t in map_response.context['import_templates']]
        self.assertNotIn('My Private Mapping', names)

    def test_saved_shared_template_is_visible_to_other_users(self):
        self._upload([['Widget', '', '', '', '10', '', '', '', '', '']])
        payload = dict(self.MAPPING, save_template_name='Team Mapping',
                       template_visibility='shared')
        self.client.post(reverse('product_import_map'), payload)

        other = User.objects.create_user(
            username='manager3', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(other)
        self._upload([['Widget', '', '', '', '10', '', '', '', '', '']])
        map_response = self.client.get(reverse('product_import_map'), follow=True)
        names = [t.name for t in map_response.context['import_templates']]
        self.assertIn('Team Mapping', names)

    # ── Cancel ──────────────────────────────────────────────

    def test_cancel_clears_the_in_progress_import(self):
        self._upload([['Widget', '', '', '', '10', '', '', '', '', '']])
        self.assertIn('product_import', self.client.session)
        response = self.client.post(reverse('product_import_cancel'))
        self.assertRedirects(response, reverse('product_list'))
        self.assertNotIn('product_import', self.client.session)

    def test_preview_without_an_uploaded_file_redirects_to_upload(self):
        response = self.client.get(reverse('product_import_preview'))
        self.assertRedirects(
            response, reverse('product_import_upload'), fetch_redirect_response=False
        )
        self.assertFalse(ProductCategory.objects.exists())


class ProductPriceTierFormTests(TestCase):
    """The bulk-price ladder on the Add/Edit Product modals, validated as a
    whole by inventory.views._parse_price_tiers. A bad ladder mis-bills
    quietly rather than crashing, so it is refused here, where it can still be
    explained, and nothing is saved."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)

    def _add(self, quantities, prices, unit_price='54'):
        return self.client.post(reverse('product_add'), {
            'product_name': 'Rice (per kg)',
            'unit_of_measure': Product.UnitOfMeasure.KILO,
            'unit_price': unit_price,
            'tier_min_quantity': quantities,
            'tier_price': prices,
        })

    @staticmethod
    def _ladder(product):
        return list(product.price_tiers.values_list('min_quantity', 'unit_price'))

    def _assert_refused(self, response, fragment):
        self.assertEqual(response.status_code, 200)
        errors = response.context['add_errors']
        self.assertTrue(any(fragment in e for e in errors), errors)
        self.assertFalse(Product.objects.filter(product_name='Rice (per kg)').exists())

    def test_add_saves_a_three_step_ladder(self):
        response = self._add(['10', '25', '50'], ['48', '46', '45'])
        self.assertRedirects(response, reverse('product_list'))
        product = Product.objects.get(product_name='Rice (per kg)')
        self.assertEqual(self._ladder(product), [(10, 48), (25, 46), (50, 45)])

    def test_rows_are_stored_in_quantity_order_whatever_order_they_were_typed(self):
        self._add(['50', '10'], ['45', '48'])
        product = Product.objects.get(product_name='Rice (per kg)')
        self.assertEqual(self._ladder(product), [(10, 48), (50, 45)])

    def test_blank_spare_rows_are_ignored(self):
        response = self._add(['10', '', ''], ['48', '', ''])
        self.assertRedirects(response, reverse('product_list'))
        product = Product.objects.get(product_name='Rice (per kg)')
        self.assertEqual(self._ladder(product), [(10, 48)])

    def test_a_tier_at_or_above_retail_is_refused(self):
        self._assert_refused(self._add(['10'], ['54']), 'not below the retail price')

    def test_prices_must_fall_as_quantities_rise(self):
        self._assert_refused(
            self._add(['10', '50'], ['46', '48']), 'must be lower than the price at 10+'
        )

    def test_a_break_at_quantity_one_is_refused(self):
        self._assert_refused(self._add(['1'], ['50']), '2 or more')

    def test_a_half_filled_row_is_refused(self):
        self._assert_refused(self._add(['10'], ['']), 'fill in both')

    def test_a_duplicate_quantity_is_refused(self):
        self._assert_refused(
            self._add(['10', '10'], ['48', '47']), 'already a break at 10'
        )

    def test_a_refused_save_hands_the_typed_rows_back(self):
        response = self._add(['10', '50'], ['46', '48'])
        self.assertEqual(response.context['add_tier_rows'], [
            {'min_quantity': '10', 'unit_price': '46'},
            {'min_quantity': '50', 'unit_price': '48'},
        ])

    def test_edit_replaces_the_ladder_and_a_cleared_row_disappears(self):
        product = Product.objects.create(
            sku_code='SKU-T1', product_name='Sugar', unit_price=60,
            unit_of_measure=Product.UnitOfMeasure.KILO,
        )
        product.price_tiers.create(min_quantity=10, unit_price=55)
        product.price_tiers.create(min_quantity=50, unit_price=52)

        response = self.client.post(reverse('product_edit', args=[product.pk]), {
            'product_name': 'Sugar',
            'unit_of_measure': Product.UnitOfMeasure.KILO,
            'unit_price': '60',
            'tier_min_quantity': ['10', ''],
            'tier_price': ['54', ''],
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._ladder(product), [(10, 54)])

    def test_a_refused_edit_leaves_the_stored_ladder_untouched(self):
        product = Product.objects.create(
            sku_code='SKU-T2', product_name='Flour', unit_price=40,
            unit_of_measure=Product.UnitOfMeasure.KILO,
        )
        product.price_tiers.create(min_quantity=10, unit_price=36)

        response = self.client.post(reverse('product_edit', args=[product.pk]), {
            'product_name': 'Flour',
            'unit_of_measure': Product.UnitOfMeasure.KILO,
            'unit_price': '40',
            'tier_min_quantity': ['10'],
            'tier_price': ['45'],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ladder(product), [(10, 36)])


class BatchInventoryReconciliationTests(TestCase):
    """O-1 — batch_inventory_mismatches (and the management command that
    surfaces it) flags a row only when it HAS batches that disagree with its
    own aggregate; a row with no batches at all predates batch tracking and
    is not a mismatch (see the Batch model's own docstring)."""

    def setUp(self):
        self.branch = Branch.objects.create(branch_name='Tarcan', location='Baliuag')
        self.product = Product.objects.create(
            sku_code='SKU-REC', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('100.00'),
        )

    def _floor_batch(self, quantity):
        return Batch.objects.create(
            product=self.product, branch=self.branch,
            location=Batch.Location.FLOOR,
            quantity=quantity, received_date=date.today(),
        )

    def test_agreeing_floor_row_is_not_flagged(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=20,
        )
        self._floor_batch(20)

        self.assertEqual(batch_inventory_mismatches(), [])

    def test_disagreeing_floor_row_is_flagged(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=20,
        )
        self._floor_batch(15)

        mismatches = batch_inventory_mismatches()

        self.assertEqual(len(mismatches), 1)
        row = mismatches[0]
        self.assertEqual(row['location'], 'Floor')
        self.assertEqual(row['product'], self.product)
        self.assertEqual(row['quantity_on_hand'], 20)
        self.assertEqual(row['batch_total'], 15)
        self.assertEqual(row['difference'], 5)

    def test_a_row_with_no_batches_at_all_is_not_flagged(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=20,
        )

        self.assertEqual(batch_inventory_mismatches(), [])

    def test_disagreeing_warehouse_row_is_flagged(self):
        # branches.signals auto-creates one Warehouse per Branch on save.
        warehouse = Warehouse.objects.get(branch=self.branch)
        WarehouseStock.objects.create(
            warehouse=warehouse, product=self.product, quantity_on_hand=50,
        )
        Batch.objects.create(
            product=self.product, branch=self.branch,
            location=Batch.Location.WAREHOUSE,
            quantity=40, received_date=date.today(),
        )

        mismatches = batch_inventory_mismatches()

        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]['location'], 'Warehouse')
        self.assertEqual(mismatches[0]['difference'], 10)

    def test_command_succeeds_when_nothing_is_wrong(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=20,
        )
        self._floor_batch(20)

        out = StringIO()
        call_command('check_batch_reconciliation', stdout=out)

        self.assertIn('No batch/inventory mismatches found', out.getvalue())

    def test_command_exits_with_an_error_when_something_disagrees(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=20,
        )
        self._floor_batch(15)

        with self.assertRaises(CommandError):
            call_command('check_batch_reconciliation', stdout=StringIO())


class AlertsCenterLowStockActionTests(TestCase):
    """The Low Floor Stock tab's row action restocks from the branch's own
    warehouse — it must send staff to Warehouse Stock's own Request Stock to
    Floor modal, not New Stock Adjustment (that's for write-offs/corrections,
    a different action entirely that just happened to be wired here)."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='alerts-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Low Stock Branch', location='Baliuag',
        )
        self.product = Product.objects.create(
            sku_code='SKU-LOW', product_name='Low Stock Product', unit_price=10,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=1, reorder_level=10,
        )
        self.client.force_login(self.owner)

    def test_the_row_action_opens_the_request_to_floor_modal_for_that_branch(self):
        response = self.client.get(reverse('alerts_center'))

        self.assertContains(response, 'Request Stock to Floor')
        expected_href = (
            f"{reverse('warehouse_stock_list')}?open_request=1&amp;branch={self.branch.pk}"
        )
        self.assertContains(response, expected_href)

    def test_following_the_link_lands_on_the_modal_pre_scoped_to_the_branch(self):
        url = f"{reverse('warehouse_stock_list')}?open_request=1&branch={self.branch.pk}"

        response = self.client.get(url)

        self.assertTrue(response.context['show_request_modal'])
        self.assertEqual(response.context['request_target_branch'], self.branch)
