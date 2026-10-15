"""Branch scope bar on Warehouse Stock — the same bar Floor Stock carries.

The bar is a UI convenience over scope_queryset(); these tests keep the two
separable: the tabs are only rendered for an account that may use them, counts
are per branch, the table follows the selected tab, and hand-typing ?branch=
past the bar still returns nothing for a restricted account.
"""
from datetime import date

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Batch, Product, WarehouseStock


class WarehouseStockScopeBarTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='wsb-owner', password='pw', role=User.Role.OWNER,
        )
        cls.alpha = Branch.objects.create(owner=cls.owner, branch_name='Alpha Mart', location='A')
        cls.beta = Branch.objects.create(owner=cls.owner, branch_name='Beta Mart', location='B')
        cls.rice = cls._product('RICE', 'Rice 5kg')
        cls.tuna = cls._product('TUNA', 'Tuna 155g')
        cls._stock(cls.alpha, cls.rice, qty=50, reorder=10)
        cls._stock(cls.alpha, cls.tuna, qty=0, reorder=10)   # No Stock -> alert
        cls._stock(cls.beta, cls.rice, qty=40, reorder=10)

        cls.manager = User.objects.create_user(
            username='wsb-mgr', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        cls.manager.manager_profile.branch = cls.alpha
        cls.manager.manager_profile.save(update_fields=['branch'])

    @staticmethod
    def _product(sku, name):
        return Product.objects.create(sku_code=sku, product_name=name, unit_price='10.00')

    @staticmethod
    def _stock(branch, product, qty, reorder):
        WarehouseStock.objects.create(
            warehouse=branch.warehouse, product=product,
            quantity_on_hand=qty, reorder_level=reorder,
        )
        if qty:
            Batch.objects.create(
                product=product, branch=branch, location=Batch.Location.WAREHOUSE,
                quantity=qty, received_date=date.today(), expiry_date=date(2035, 1, 1),
            )

    def get(self, user, query=''):
        self.client.force_login(user)
        return self.client.get(reverse('warehouse_stock_list') + query)

    def segments(self, response):
        return {s['name']: s for s in response.context['scope_branches']}

    def test_owner_gets_all_branches_plus_one_tab_per_branch(self):
        response = self.get(self.owner)
        html = response.content.decode()

        self.assertIn('class="seg"', html)
        self.assertIn('All Branches', html)
        for branch in Branch.objects.all():
            self.assertIn(branch.branch_name, html)

    def test_counts_are_per_branch_and_all_branches_is_their_sum(self):
        by_name = self.segments(self.get(self.owner))

        self.assertEqual(by_name['Alpha Mart']['total'], 2)
        self.assertEqual(by_name['Beta Mart']['total'], 1)
        self.assertEqual(by_name['All Branches']['total'], WarehouseStock.objects.count())
        # Alpha's tuna is out of stock, so its tab is flagged.
        self.assertEqual(by_name['Alpha Mart']['alerts'], 1)
        self.assertEqual(by_name['Beta Mart']['alerts'], 0)

    def test_all_branches_is_active_by_default_and_branch_tab_when_selected(self):
        by_name = self.segments(self.get(self.owner))
        self.assertTrue(by_name['All Branches']['active'])
        self.assertFalse(by_name['Alpha Mart']['active'])

        by_name = self.segments(self.get(self.owner, f'?branch={self.alpha.pk}'))
        self.assertFalse(by_name['All Branches']['active'])
        self.assertTrue(by_name['Alpha Mart']['active'])
        self.assertContains(
            self.client.get(reverse('warehouse_stock_list') + f'?branch={self.alpha.pk}'),
            'aria-current="page"',
        )

    def test_selecting_a_branch_filters_the_table_but_not_the_tab_counts(self):
        response = self.get(self.owner, f'?branch={self.beta.pk}')

        rows = response.context['rows']
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(r['stock'].warehouse.branch_id == self.beta.pk for r in rows))
        # KPI cards follow the selected branch...
        self.assertEqual(response.context['counts']['in_stock'], 1)
        self.assertEqual(response.context['counts']['no_stock'], 0)
        # ...while every tab still reports what it would return.
        by_name = self.segments(response)
        self.assertEqual(by_name['Alpha Mart']['total'], 2)
        self.assertEqual(by_name['All Branches']['total'], 3)

    def test_tab_links_carry_search_and_category_across(self):
        by_name = self.segments(self.get(self.owner, '?q=rice'))

        self.assertIn('q=rice', by_name['Beta Mart']['href'])
        self.assertIn(f'branch={self.beta.pk}', by_name['Beta Mart']['href'])
        self.assertNotIn('branch=', by_name['All Branches']['href'])
        # Search narrows what each tab counts, too.
        self.assertEqual(by_name['Alpha Mart']['total'], 1)
        self.assertEqual(by_name['Beta Mart']['total'], 1)

    def test_search_form_keeps_the_selected_branch(self):
        html = self.get(self.owner, f'?branch={self.alpha.pk}').content.decode()
        self.assertIn(f'<input type="hidden" name="branch" value="{self.alpha.pk}">', html)

    def test_manager_gets_no_tabs_only_their_branch(self):
        response = self.get(self.manager)
        html = response.content.decode()

        self.assertNotIn('class="seg"', html)
        self.assertIn('locked-branch', html)
        self.assertNotIn(self.beta.branch_name, html)
        self.assertEqual(response.context['scope_branches'], [])

    def test_manager_typing_another_branch_id_still_sees_only_their_own_stock(self):
        """The bar is not the security boundary."""
        response = self.get(self.manager, f'?branch={self.beta.pk}')

        self.assertEqual(response.status_code, 200)
        rows = response.context['rows']
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(r['stock'].warehouse.branch_id == self.alpha.pk for r in rows))

    def test_unassigned_manager_sees_neither_tabs_nor_rows(self):
        self.manager.manager_profile.branch = None
        self.manager.manager_profile.save(update_fields=['branch'])

        response = self.get(self.manager)
        html = response.content.decode()

        self.assertNotIn('class="seg"', html)
        self.assertEqual(len(response.context['rows']), 0)
        self.assertIn('No branch is assigned to your account', html)
