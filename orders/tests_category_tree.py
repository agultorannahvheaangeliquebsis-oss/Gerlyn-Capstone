"""The two-level category tree, on the storefront and in the catalog.

Covers the behaviour the shape actually buys, not the markup:

  * departments carry their own shelves, and only categories with something
    in them are drawn at all;
  * ticking a department means its shelves too, so a department link is not
    a link to an empty grid;
  * the ticks in the rail stay the shopper's own answer, and are not
    back-filled with the children the filter expanded to;
  * the rail's counts are the grid's counts;
  * the removable chips drop exactly one filter and keep the rest;
  * the model refuses a third level, in both directions.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.context_processors import storefront_categories, storefront_category_ids
from inventory.models import Inventory, Product, ProductCategory


class CategoryTreeTestCase(TestCase):
    """A two-department store: Fresh (two shelves) and Rice (one)."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='tree-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )

        self.fresh = ProductCategory.objects.create(category_name='Fresh')
        self.beef = ProductCategory.objects.create(
            category_name='Fresh Beef', parent=self.fresh
        )
        self.chicken = ProductCategory.objects.create(
            category_name='Fresh Chicken', parent=self.fresh
        )
        self.rice_dept = ProductCategory.objects.create(category_name='Rice & Grains')

        self.sirloin = self._stock('SKU-BEEF', 'Sirloin', self.beef, '340.00')
        self.thigh = self._stock('SKU-CHIX', 'Chicken Thigh', self.chicken, '180.00')
        # Filed directly on a department rather than on a shelf — a normal
        # thing in a variety store, and the case a naive "sum the children"
        # count would lose.
        self.sack = self._stock('SKU-RICE', 'Rice 25kg', self.rice_dept, '1250.00')

    def _stock(self, sku, name, category, price, qty=10):
        product = Product.objects.create(
            sku_code=sku, product_name=name, category=category,
            unit_of_measure='Piece', unit_price=Decimal(price),
        )
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=qty, reorder_level=1,
        )
        return product


class StorefrontCategoryTreeTests(CategoryTreeTestCase):

    def test_departments_carry_their_own_shelves(self):
        tree = storefront_categories()

        self.assertEqual(
            [d.category_name for d in tree], ['Fresh', 'Rice & Grains']
        )
        fresh, rice = tree
        self.assertEqual(
            [s.category_name for s in fresh.subcategories],
            ['Fresh Beef', 'Fresh Chicken'],
        )
        self.assertEqual(rice.subcategories, [])

    def test_a_department_with_only_stocked_shelves_is_drawn(self):
        """Fresh holds no products of its own — its shelves do."""
        self.assertIn('Fresh', [d.category_name for d in storefront_categories()])

    def test_an_empty_department_is_not_drawn(self):
        ProductCategory.objects.create(category_name='Frozen')

        self.assertNotIn('Frozen', [d.category_name for d in storefront_categories()])

    def test_an_empty_shelf_is_not_drawn_under_a_stocked_department(self):
        ProductCategory.objects.create(category_name='Fresh Pork', parent=self.fresh)

        fresh = storefront_categories()[0]

        self.assertEqual(
            [s.category_name for s in fresh.subcategories],
            ['Fresh Beef', 'Fresh Chicken'],
        )

    def test_one_department_with_shelves_clears_the_threshold(self):
        """Shelves are choices too — the rule counts what can be picked."""
        self.rice_dept.delete()
        self.sack.delete()

        tree = storefront_categories()

        self.assertEqual([d.category_name for d in tree], ['Fresh'])

    def test_a_lone_category_with_no_shelves_is_still_hidden(self):
        for obj in (self.beef, self.chicken, self.rice_dept):
            Product.objects.filter(category=obj).delete()
            obj.delete()

        self.assertEqual(storefront_categories(), [])

    def test_limit_caps_departments_not_shelves(self):
        tree = storefront_categories(limit=2)

        self.assertEqual(len(tree), 2)
        self.assertEqual(len(tree[0].subcategories), 2)

    def test_limit_below_two_is_refused(self):
        with self.assertRaises(ValueError):
            storefront_categories(limit=1)

    def test_expanding_a_department_picks_up_its_shelves(self):
        expanded = storefront_category_ids([str(self.fresh.pk)])

        self.assertEqual(
            set(expanded),
            {str(self.fresh.pk), str(self.beef.pk), str(self.chicken.pk)},
        )

    def test_expanding_a_shelf_is_just_the_shelf(self):
        self.assertEqual(
            storefront_category_ids([str(self.beef.pk)]), [str(self.beef.pk)]
        )


class CatalogFilteringTests(CategoryTreeTestCase):

    def test_ticking_a_department_lists_everything_under_it(self):
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.fresh.pk, 'branch': ''}
        )

        listed = {row['product'].sku_code for row in response.context['rows']}
        self.assertEqual(listed, {'SKU-BEEF', 'SKU-CHIX'})

    def test_ticking_a_shelf_lists_only_that_shelf(self):
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.beef.pk, 'branch': ''}
        )

        listed = {row['product'].sku_code for row in response.context['rows']}
        self.assertEqual(listed, {'SKU-BEEF'})

    def test_the_shelves_a_department_expanded_to_are_not_ticked(self):
        """The expansion is a query detail; the boxes stay the shopper's."""
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.fresh.pk, 'branch': ''}
        )

        self.assertEqual(response.context['category_filters'], [str(self.fresh.pk)])

    def test_the_rail_counts_match_the_grid(self):
        response = self.client.get(reverse('orders:catalog'), {'branch': ''})

        rail = {d.category_name: d.stock_count for d in response.context['categories']}
        self.assertEqual(rail, {'Fresh': 2, 'Rice & Grains': 1})

        fresh = next(
            d for d in response.context['categories'] if d.category_name == 'Fresh'
        )
        self.assertEqual(
            {s.category_name: s.stock_count for s in fresh.shelves},
            {'Fresh Beef': 1, 'Fresh Chicken': 1},
        )

    def test_a_department_counts_its_own_products_and_its_shelves(self):
        self._stock('SKU-LOOSE', 'Rice 5kg', self.rice_dept, '280.00')
        shelf = ProductCategory.objects.create(
            category_name='Sacks', parent=self.rice_dept
        )
        self._stock('SKU-SACK50', 'Rice 50kg', shelf, '2400.00')

        response = self.client.get(reverse('orders:catalog'), {'branch': ''})

        rice = next(
            d for d in response.context['categories']
            if d.category_name == 'Rice & Grains'
        )
        self.assertEqual(rice.stock_count, 3)

    def test_out_of_stock_shelves_are_dropped_from_the_rail(self):
        Inventory.objects.filter(product=self.thigh).update(quantity_on_hand=0)

        response = self.client.get(reverse('orders:catalog'), {'branch': ''})

        fresh = next(
            d for d in response.context['categories'] if d.category_name == 'Fresh'
        )
        self.assertEqual([s.category_name for s in fresh.shelves], ['Fresh Beef'])
        self.assertEqual(fresh.stock_count, 1)

    def test_the_breadcrumb_names_a_single_ticked_category(self):
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.beef.pk, 'branch': ''}
        )

        self.assertEqual(
            [c.pk for c in response.context['active_categories']], [self.beef.pk]
        )
        self.assertContains(response, 'Fresh Beef')

    def test_a_shelf_chip_is_named_with_its_department(self):
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.beef.pk, 'branch': ''}
        )

        chips = response.context['active_chips']
        self.assertEqual([c['label'] for c in chips], ['Category'])
        self.assertEqual(chips[0]['value'], 'Fresh › Fresh Beef')

    def test_removing_one_chip_keeps_the_other_filters(self):
        response = self.client.get(reverse('orders:catalog'), {
            'category': [self.beef.pk, self.chicken.pk], 'branch': ''
        })

        chips = response.context['active_chips']
        beef_chip = next(c for c in chips if 'Fresh Beef' in c['value'])

        self.assertIn(f'category={self.chicken.pk}', beef_chip['remaining_qs'])
        self.assertNotIn(f'category={self.beef.pk}', beef_chip['remaining_qs'])

    def test_every_chip_link_keeps_an_explicit_branch_key(self):
        """Without it the view falls back to the session's shopping branch."""
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session.save()

        response = self.client.get(reverse('orders:catalog'), {
            'category': self.beef.pk, 'branch': self.branch.pk
        })

        for chip in response.context['active_chips']:
            self.assertIn('branch=', chip['remaining_qs'])

    def test_removing_the_last_branch_chip_shows_every_branch(self):
        other = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        elsewhere = Product.objects.create(
            sku_code='SKU-OTHER', product_name='Sardines',
            category=self.rice_dept, unit_of_measure='Can',
            unit_price=Decimal('26.00'),
        )
        Inventory.objects.create(
            product=elsewhere, branch=other, quantity_on_hand=5, reorder_level=1
        )

        response = self.client.get(reverse('orders:catalog'), {'branch': self.branch.pk})
        chip = next(c for c in response.context['active_chips'] if c['label'] == 'Branch')

        after = self.client.get(f"{reverse('orders:catalog')}?{chip['remaining_qs']}")

        listed = {row['product'].sku_code for row in after.context['rows']}
        self.assertIn('SKU-OTHER', listed)

    def test_a_search_chip_appears_and_can_be_removed(self):
        response = self.client.get(
            reverse('orders:catalog'), {'q': 'Sirloin', 'branch': ''}
        )

        chip = next(c for c in response.context['active_chips'] if c['label'] == 'Search')
        self.assertEqual(chip['value'], 'Sirloin')
        self.assertNotIn('q=', chip['remaining_qs'])

    def test_no_filters_means_no_chips(self):
        response = self.client.get(reverse('orders:catalog'))

        self.assertEqual(response.context['active_chips'], [])

    def test_live_search_still_answers_json(self):
        """The rail is not rebuilt on the XHR path — it must not 500 either."""
        response = self.client.get(
            reverse('orders:catalog'), {'q': 'Sirloin'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('grid_html', response.json())


class CategoryDepthTests(TestCase):
    """Two levels, enforced on the model so every writer gets the rule."""

    def setUp(self):
        self.fresh = ProductCategory.objects.create(category_name='Fresh')
        self.beef = ProductCategory.objects.create(
            category_name='Fresh Beef', parent=self.fresh
        )

    def test_a_third_level_is_refused(self):
        wagyu = ProductCategory(category_name='Wagyu', parent=self.beef)

        with self.assertRaises(ValidationError):
            wagyu.full_clean()

    def test_a_department_with_shelves_cannot_be_filed_under_another(self):
        household = ProductCategory.objects.create(category_name='Household')
        self.fresh.parent = household

        with self.assertRaises(ValidationError):
            self.fresh.full_clean()

    def test_a_category_cannot_be_its_own_parent(self):
        self.fresh.parent = self.fresh

        with self.assertRaises(ValidationError):
            self.fresh.full_clean()

    def test_a_shelf_under_a_department_is_fine(self):
        pork = ProductCategory(category_name='Fresh Pork', parent=self.fresh)

        pork.full_clean()  # does not raise

    def test_a_top_level_category_is_fine(self):
        ProductCategory(category_name='Frozen').full_clean()  # does not raise
