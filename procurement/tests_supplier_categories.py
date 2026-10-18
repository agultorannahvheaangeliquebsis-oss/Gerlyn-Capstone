"""P20 — supplier categories are a managed list (Owner-maintained), not
free text. The supplier form picks from it; the old text column is migrated
into it."""
from django.test import TestCase
from django.urls import reverse

from accounts.models import SupplierCategory, User
from inventory.models import ProductCategory


class SupplierCategoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='p20-owner', password='pw', role=User.Role.OWNER)
        cls.supplier = User.objects.create_user(
            username='p20-supplier', password='pw', role=User.Role.SUPPLIER)
        cls.canned = SupplierCategory.objects.create(name='Canned Goods')

    def setUp(self):
        self.client.force_login(self.owner)

    def _save_supplier(self, **extra):
        data = {'supplier_name': 'Acme', 'username': 'p20-supplier'}
        data.update(extra)
        return self.client.post(
            reverse('supplier_edit', args=[self.supplier.pk]), data)

    def test_supplier_form_saves_the_chosen_category(self):
        self._save_supplier(category=self.canned.pk)
        self.supplier.supplier_profile.refresh_from_db()
        self.assertEqual(self.supplier.supplier_profile.category, self.canned)

    def test_blank_choice_clears_the_category(self):
        profile = self.supplier.supplier_profile
        profile.category = self.canned
        profile.save()
        self._save_supplier(category='')
        profile.refresh_from_db()
        self.assertIsNone(profile.category)

    def test_unknown_or_garbage_category_is_rejected_and_nothing_saved(self):
        for bad in ('99999', 'abc'):
            response = self._save_supplier(category=bad)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Choose a category from the list.')
        self.supplier.supplier_profile.refresh_from_db()
        self.assertIsNone(self.supplier.supplier_profile.category)

    def test_inactive_category_cannot_be_newly_assigned_but_can_be_kept(self):
        retired = SupplierCategory.objects.create(name='Old', is_active=False)
        response = self._save_supplier(category=retired.pk)
        self.assertContains(response, 'Choose a category from the list.')

        profile = self.supplier.supplier_profile
        profile.category = retired
        profile.save()
        self._save_supplier(category=retired.pk, supplier_name='Acme 2')
        profile.refresh_from_db()
        self.assertEqual(profile.category, retired)
        self.assertEqual(profile.supplier_name, 'Acme 2')

    def test_supplier_list_shows_the_category_name(self):
        profile = self.supplier.supplier_profile
        profile.category = self.canned
        profile.save()
        response = self.client.get(reverse('supplier_list'))
        self.assertContains(response, 'Canned Goods')

    def test_owner_can_add_edit_and_link_product_categories(self):
        pc = ProductCategory.objects.create(category_name='Pantry')
        self.client.post(reverse('supplier_category_add'), {
            'name': 'Beverages', 'product_categories': [pc.pk]})
        bev = SupplierCategory.objects.get(name='Beverages')
        self.assertTrue(bev.is_active)
        self.assertEqual(list(bev.product_categories.all()), [pc])

        self.client.post(reverse('supplier_category_edit', args=[bev.pk]), {
            'name': 'Drinks'})  # is_active unticked -> inactive, links cleared
        bev.refresh_from_db()
        self.assertEqual(bev.name, 'Drinks')
        self.assertFalse(bev.is_active)
        self.assertEqual(bev.product_categories.count(), 0)

    def test_duplicate_names_are_rejected_case_insensitively(self):
        self.client.post(reverse('supplier_category_add'), {'name': 'canned goods'})
        self.assertEqual(SupplierCategory.objects.count(), 1)

    def test_delete_is_blocked_while_suppliers_use_it(self):
        profile = self.supplier.supplier_profile
        profile.category = self.canned
        profile.save()
        self.client.post(reverse('supplier_category_delete', args=[self.canned.pk]))
        self.assertTrue(SupplierCategory.objects.filter(pk=self.canned.pk).exists())

        profile.category = None
        profile.save()
        self.client.post(reverse('supplier_category_delete', args=[self.canned.pk]))
        self.assertFalse(SupplierCategory.objects.filter(pk=self.canned.pk).exists())

    def test_only_the_owner_can_write_categories(self):
        manager = User.objects.create_user(
            username='p20-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        self.client.force_login(manager)
        self.client.post(reverse('supplier_category_add'), {'name': 'Snacks'})
        self.client.post(reverse('supplier_category_delete', args=[self.canned.pk]))
        self.assertFalse(SupplierCategory.objects.filter(name='Snacks').exists())
        self.assertTrue(SupplierCategory.objects.filter(pk=self.canned.pk).exists())
        # ...but can look at the page read-only.
        self.assertEqual(self.client.get(reverse('supplier_category_list')).status_code, 200)

    def test_page_lists_categories_as_rows_with_row_actions(self):
        unused = SupplierCategory.objects.create(name='Unused')
        profile = self.supplier.supplier_profile
        profile.category = self.canned
        profile.save()
        response = self.client.get(reverse('supplier_category_list'))
        self.assertContains(response, 'id="category-table"')
        self.assertContains(response, 'add-category-modal')
        self.assertContains(
            response, reverse('supplier_category_edit', args=[self.canned.pk]))
        # In-use category: delete is disabled; unused one gets a confirm form.
        self.assertContains(response, 'Cannot delete Canned Goods')
        self.assertContains(
            response, reverse('supplier_category_delete', args=[unused.pk]))
        self.assertNotContains(
            response, reverse('supplier_category_delete', args=[self.canned.pk]))

    def test_modals_use_tile_grid_and_active_switch_only_on_edit(self):
        pc = ProductCategory.objects.create(category_name='Pantry')
        response = self.client.get(reverse('supplier_category_list'))
        self.assertContains(response, 'class="check-tile"', count=2)  # add + edit
        self.assertContains(response, 'class="switch"', count=1)      # edit only
        self.assertContains(response, 'Tick none to show every product.')
        self.assertContains(response, 'Suppliers who already have it keep it.')
        self.assertContains(response, f'value="{pc.pk}"', count=2)

    def test_manager_sees_the_list_without_edit_controls(self):
        manager = User.objects.create_user(
            username='p20-mgr2', password='pw', role=User.Role.BRANCH_MANAGER)
        self.client.force_login(manager)
        response = self.client.get(reverse('supplier_category_list'))
        self.assertContains(response, 'Canned Goods')
        self.assertNotContains(response, 'icon-btn-action')
        self.assertNotContains(response, 'add-category-modal')

    def test_suppliers_and_customers_cannot_open_the_page(self):
        self.client.force_login(self.supplier)
        response = self.client.get(reverse('supplier_category_list'))
        self.assertEqual(response.status_code, 302)


from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class SupplierCategoryDataMigrationTests(TransactionTestCase):
    """0024 turns each distinct free-text category into a SupplierCategory,
    collapsing case-only spelling differences, before dropping the column."""

    before = [('accounts', '0023_merge_20260923_1856')]
    after = [('accounts', '0024_supplier_category_fk')]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Leave the shared test DB fully migrated for whatever runs next.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_text_categories_become_rows_and_links(self):
        old_apps = self._migrate(self.before)
        User_ = old_apps.get_model('accounts', 'User')
        Profile = old_apps.get_model('accounts', 'SupplierProfile')
        for i, text in enumerate(['Canned Goods', 'canned goods', 'Beverages', '']):
            u = User_.objects.create(username=f'dm{i}', role='Supplier', password='x')
            Profile.objects.update_or_create(
                user=u, defaults={'supplier_name': f'S{i}', 'supplier_category': text})

        new_apps = self._migrate(self.after)
        Category = new_apps.get_model('accounts', 'SupplierCategory')
        Profile = new_apps.get_model('accounts', 'SupplierProfile')

        self.assertEqual(
            sorted(Category.objects.values_list('name', flat=True)),
            ['Beverages', 'Canned Goods'])
        cats = {p.supplier_name: (p.category.name if p.category else None)
                for p in Profile.objects.filter(supplier_name__in=['S0', 'S1', 'S2', 'S3'])}
        self.assertEqual(cats, {
            'S0': 'Canned Goods', 'S1': 'Canned Goods', 'S2': 'Beverages', 'S3': None})
