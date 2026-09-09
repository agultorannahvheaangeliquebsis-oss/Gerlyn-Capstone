import io
from decimal import Decimal
from io import StringIO

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import Product, ProductCategory, ProductImportMappingTemplate


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

    HEADER = [
        'Item Name', 'Barcode', 'Cat', 'UOM', 'SRP',
        'Wholesale', 'Notes', 'VAT', 'PWD', 'Senior',
    ]
    MAPPING = {
        'map_product_name': 'Item Name',
        'map_barcode': 'Barcode',
        'map_category': 'Cat',
        'map_unit_of_measure': 'UOM',
        'map_unit_price': 'SRP',
        'map_wholesale_price': 'Wholesale',
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
        Product.objects.create(
            sku_code='SKU-EXPORT-1', product_name='Chippy', barcode='EXPORT001',
            category=category, unit_of_measure=Product.UnitOfMeasure.PIECE,
            unit_price=15, wholesale_price=12,
        )
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
        self.assertRedirects(response, reverse('product_import_map'))
        map_response = self.client.get(reverse('product_import_map'))
        fields_by_key = {f['key']: f for f in map_response.context['fields']}
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

        preview = self.client.get(reverse('product_import_preview'))
        self.assertEqual(preview.context['counts'], {
            'total': 6, 'create': 2, 'update': 1, 'error': 3,
        })
        created_action = next(r for r in preview.context['rows'] if r['barcode'] == 'NEWBAR001')
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

    # ── Saved mapping templates ─────────────────────────────

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
        map_response = self.client.get(reverse('product_import_map'))
        names = [t.name for t in map_response.context['templates']]
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
        map_response = self.client.get(reverse('product_import_map'))
        names = [t.name for t in map_response.context['templates']]
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
        self.assertRedirects(response, reverse('product_import_upload'))
        self.assertFalse(ProductCategory.objects.exists())
