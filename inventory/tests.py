import io
from datetime import date
from decimal import Decimal
from io import StringIO

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
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
        changed, so this is here to catch it if the extraction broke it."""
        response = self.client.get(reverse('product_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'add-product-modal')

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
            response, reverse('category_list'), fetch_redirect_response=False
        )
        self.assertFalse(
            ProductCategory.objects.filter(category_name='Canned Goods').exists()
        )


class CategoryParentTests(TestCase):
    """Categories are two levels deep — a department and its shelves — and
    the storefront's Shop mega-menu is built off that. These cover the half
    of the rule the model cannot enforce on its own: what the form offers,
    what it refuses, and what deleting a department does."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='parent-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)
        self.fresh = ProductCategory.objects.create(category_name='Fresh')

    def test_post_files_a_new_category_under_a_parent(self):
        self.client.post(reverse('category_add'), {
            'category_name': 'Fresh Beef', 'parent': self.fresh.pk,
        })

        beef = ProductCategory.objects.get(category_name='Fresh Beef')
        self.assertEqual(beef.parent, self.fresh)

    def test_an_empty_parent_creates_a_department(self):
        self.client.post(reverse('category_add'), {
            'category_name': 'Household', 'parent': '',
        })

        self.assertIsNone(
            ProductCategory.objects.get(category_name='Household').parent
        )

    def test_a_third_level_is_rejected_with_the_modal_reopened(self):
        beef = ProductCategory.objects.create(
            category_name='Fresh Beef', parent=self.fresh
        )

        response = self.client.post(reverse('category_add'), {
            'category_name': 'Wagyu', 'parent': beef.pk,
        })

        self.assertTrue(response.context['show_add_modal'])
        self.assertTrue(response.context['add_errors'])
        self.assertFalse(
            ProductCategory.objects.filter(category_name='Wagyu').exists()
        )

    def test_an_unknown_parent_is_rejected_rather_than_assigned(self):
        response = self.client.post(reverse('category_add'), {
            'category_name': 'Frozen', 'parent': '9999',
        })

        self.assertTrue(response.context['add_errors'])
        self.assertFalse(
            ProductCategory.objects.filter(category_name='Frozen').exists()
        )

    def test_editing_can_move_a_shelf_to_another_department(self):
        household = ProductCategory.objects.create(category_name='Household')
        soap = ProductCategory.objects.create(
            category_name='Soap', parent=self.fresh
        )

        self.client.post(reverse('category_edit', args=[soap.pk]), {
            'category_name': 'Soap', 'parent': household.pk,
        })

        soap.refresh_from_db()
        self.assertEqual(soap.parent, household)

    def test_editing_can_promote_a_shelf_to_a_department(self):
        beef = ProductCategory.objects.create(
            category_name='Fresh Beef', parent=self.fresh
        )

        self.client.post(reverse('category_edit', args=[beef.pk]), {
            'category_name': 'Fresh Beef', 'parent': '',
        })

        beef.refresh_from_db()
        self.assertIsNone(beef.parent)

    def test_a_department_with_shelves_cannot_be_deleted(self):
        ProductCategory.objects.create(category_name='Fresh Beef', parent=self.fresh)

        response = self.client.post(
            reverse('category_delete', args=[self.fresh.pk]), follow=True
        )

        self.assertTrue(ProductCategory.objects.filter(pk=self.fresh.pk).exists())
        self.assertContains(response, 'sub-categor')

    def test_an_empty_department_can_still_be_deleted(self):
        self.client.post(reverse('category_delete', args=[self.fresh.pk]))

        self.assertFalse(ProductCategory.objects.filter(pk=self.fresh.pk).exists())

    def test_the_list_orders_shelves_under_their_department(self):
        household = ProductCategory.objects.create(category_name='Household')
        ProductCategory.objects.create(category_name='Aaa Soap', parent=household)
        ProductCategory.objects.create(category_name='Zzz Beef', parent=self.fresh)

        response = self.client.get(reverse('category_list'))

        # Alphabetical by department, each followed by its own shelves —
        # NOT one flat alphabetical list, in which "Aaa Soap" would sort
        # above the "Household" it belongs to.
        self.assertEqual(
            [(c.category_name, c.depth) for c in response.context['categories']],
            [('Fresh', 0), ('Zzz Beef', 1), ('Household', 0), ('Aaa Soap', 1)],
        )

    def test_the_parent_picker_offers_departments_only(self):
        ProductCategory.objects.create(category_name='Fresh Beef', parent=self.fresh)

        response = self.client.get(reverse('category_list'))

        self.assertEqual(
            [c.category_name for c in response.context['departments']], ['Fresh']
        )


class CategoryApiParentTests(TestCase):
    """/api/categories/ is a writer too, so the two-levels rule has to hold
    there as well — otherwise it is the one path that can build a third level
    the storefront then cannot draw."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='api-owner', password='pw', role=User.Role.OWNER
        )
        self.client.force_login(self.owner)
        self.fresh = ProductCategory.objects.create(category_name='Fresh')
        self.beef = ProductCategory.objects.create(
            category_name='Fresh Beef', parent=self.fresh
        )

    def test_list_exposes_the_parent(self):
        response = self.client.get('/api/categories/')

        rows = response.json()
        rows = rows['results'] if isinstance(rows, dict) else rows
        beef = next(r for r in rows if r['categoryName'] == 'Fresh Beef')
        self.assertEqual(beef['parentId'], self.fresh.pk)
        self.assertEqual(beef['parentName'], 'Fresh')

    def test_a_department_reports_no_parent(self):
        response = self.client.get('/api/categories/')

        rows = response.json()
        rows = rows['results'] if isinstance(rows, dict) else rows
        fresh = next(r for r in rows if r['categoryName'] == 'Fresh')
        self.assertIsNone(fresh['parentId'])

    def test_post_can_file_a_shelf_under_a_department(self):
        response = self.client.post(
            '/api/categories/',
            {'categoryName': 'Fresh Pork', 'parentId': self.fresh.pk},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            ProductCategory.objects.get(category_name='Fresh Pork').parent,
            self.fresh,
        )

    def test_post_refuses_a_third_level(self):
        response = self.client.post(
            '/api/categories/',
            {'categoryName': 'Wagyu', 'parentId': self.beef.pk},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            ProductCategory.objects.filter(category_name='Wagyu').exists()
        )

    def test_patch_refuses_to_bury_a_department_that_has_shelves(self):
        household = ProductCategory.objects.create(category_name='Household')

        response = self.client.patch(
            f'/api/categories/{self.fresh.pk}/',
            {'parentId': household.pk},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.fresh.refresh_from_db()
        self.assertIsNone(self.fresh.parent)

    def test_delete_refuses_a_department_that_still_has_shelves(self):
        """parent is PROTECT — deleting a department with shelves under it
        must answer with a clean 400, not an unhandled ProtectedError."""
        response = self.client.delete(f'/api/categories/{self.fresh.pk}/')

        self.assertEqual(response.status_code, 400)
        self.assertTrue(ProductCategory.objects.filter(pk=self.fresh.pk).exists())

    def test_delete_removes_a_childless_shelf(self):
        response = self.client.delete(f'/api/categories/{self.beef.pk}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(ProductCategory.objects.filter(pk=self.beef.pk).exists())


class SeedCategoriesCommandTests(TestCase):
    """seed_categories runs against a live catalog, unlike seed_demo which
    wipes one first — so what matters is that it adds without destroying and
    that a second run is a no-op."""

    def test_it_builds_a_two_level_tree(self):
        call_command('seed_categories', stdout=StringIO())

        canned = ProductCategory.objects.get(category_name='Canned Goods')
        sardines = ProductCategory.objects.get(category_name='Sardines & Fish')

        self.assertIsNone(canned.parent)
        self.assertEqual(sardines.parent, canned)

    def test_running_it_twice_creates_nothing_new(self):
        call_command('seed_categories', stdout=StringIO())
        before = ProductCategory.objects.count()

        call_command('seed_categories', stdout=StringIO())

        self.assertEqual(ProductCategory.objects.count(), before)

    def test_dry_run_writes_nothing(self):
        call_command('seed_categories', '--dry-run', stdout=StringIO())

        self.assertEqual(ProductCategory.objects.count(), 0)

    def test_it_adopts_a_category_the_owner_already_typed_in(self):
        """Same name, no duplicate row — it is filed, not cloned."""
        existing = ProductCategory.objects.create(category_name='Biscuits')

        call_command('seed_categories', stdout=StringIO())

        existing.refresh_from_db()
        self.assertEqual(existing.parent.category_name, 'Snacks & Sweets')
        self.assertEqual(
            ProductCategory.objects.filter(category_name='Biscuits').count(), 1
        )

    def test_it_leaves_an_existing_filing_alone(self):
        household = ProductCategory.objects.create(category_name='Household')
        ProductCategory.objects.create(category_name='Biscuits', parent=household)

        call_command('seed_categories', stdout=StringIO())

        self.assertEqual(
            ProductCategory.objects.get(category_name='Biscuits').parent, household
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
            response, reverse('product_list'), fetch_redirect_response=False
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
