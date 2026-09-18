"""Flat categories, on the storefront and in the catalog.

Covers the behaviour categories actually buy, not the markup:

  * only categories with something in them are drawn at all;
  * the storefront hides its category UI below two pickable categories;
  * the rail's counts are the grid's counts, and are not narrowed by the
    category filter itself;
  * a ticked category stays visible at (0) through a live facet refresh
    instead of silently vanishing;
  * the removable chips drop exactly one filter and keep the rest.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from core.context_processors import storefront_categories
from inventory.models import Inventory, Product, ProductCategory


class CategoryRailTestCase(TestCase):
    """A three-category store: Fresh Beef, Fresh Chicken, Rice & Grains."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='rail-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )

        self.beef = ProductCategory.objects.create(category_name='Fresh Beef')
        self.chicken = ProductCategory.objects.create(category_name='Fresh Chicken')
        self.rice = ProductCategory.objects.create(category_name='Rice & Grains')

        self.sirloin = self._stock('SKU-BEEF', 'Sirloin', self.beef, '340.00')
        self.thigh = self._stock('SKU-CHIX', 'Chicken Thigh', self.chicken, '180.00')
        self.sack = self._stock('SKU-RICE', 'Rice 25kg', self.rice, '1250.00')

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


class StorefrontCategoriesTests(CategoryRailTestCase):

    def test_categories_with_stock_are_drawn(self):
        tree = storefront_categories()

        self.assertEqual(
            [c.category_name for c in tree],
            ['Fresh Beef', 'Fresh Chicken', 'Rice & Grains'],
        )

    def test_an_empty_category_is_not_drawn(self):
        ProductCategory.objects.create(category_name='Frozen')

        self.assertNotIn('Frozen', [c.category_name for c in storefront_categories()])

    def test_the_threshold_needs_at_least_two_pickable_categories(self):
        self.chicken.delete()
        self.thigh.delete()
        self.rice.delete()
        self.sack.delete()

        self.assertEqual(storefront_categories(), [])

    def test_limit_caps_the_list(self):
        tree = storefront_categories(limit=2)

        self.assertEqual(len(tree), 2)

    def test_limit_below_two_is_refused(self):
        with self.assertRaises(ValueError):
            storefront_categories(limit=1)


class CatalogFilteringTests(CategoryRailTestCase):

    def test_ticking_a_category_lists_only_that_category(self):
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.beef.pk, 'branch': ''}
        )

        listed = {row['product'].sku_code for row in response.context['rows']}
        self.assertEqual(listed, {'SKU-BEEF'})

    def test_the_rail_counts_match_the_grid(self):
        response = self.client.get(reverse('orders:catalog'), {'branch': ''})

        rail = {c.category_name: c.stock_count for c in response.context['categories']}
        self.assertEqual(
            rail, {'Fresh Beef': 1, 'Fresh Chicken': 1, 'Rice & Grains': 1}
        )

    def test_out_of_stock_categories_are_dropped_from_the_rail(self):
        Inventory.objects.filter(product=self.thigh).update(quantity_on_hand=0)

        response = self.client.get(reverse('orders:catalog'), {'branch': ''})

        rail = {c.category_name for c in response.context['categories']}
        self.assertNotIn('Fresh Chicken', rail)

    def test_the_breadcrumb_names_a_single_ticked_category(self):
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.beef.pk, 'branch': ''}
        )

        self.assertEqual(
            [c.pk for c in response.context['active_categories']], [self.beef.pk]
        )
        self.assertContains(response, 'Fresh Beef')

    def test_a_chip_is_named_with_the_plain_category_name(self):
        response = self.client.get(
            reverse('orders:catalog'), {'category': self.beef.pk, 'branch': ''}
        )

        chips = response.context['active_chips']
        self.assertEqual([c['label'] for c in chips], ['Category'])
        self.assertEqual(chips[0]['value'], 'Fresh Beef')

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
            category=self.rice, unit_of_measure='Can',
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

    def test_live_search_answers_all_live_regions_as_json(self):
        """XHR returns the grid, summary, and recalculated category facet."""
        response = self.client.get(
            reverse('orders:catalog'), {'q': 'Sirloin'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn('grid_html', payload)
        self.assertIn('category_html', payload)

    def test_live_response_contains_branch_filtered_category_facet(self):
        other = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        Inventory.objects.create(
            product=self.sirloin, branch=other,
            quantity_on_hand=4, reorder_level=1,
        )

        response = self.client.get(
            reverse('orders:catalog'), {'branch': other.pk},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        payload = response.json()
        self.assertIn('Fresh Beef', payload['category_html'])
        self.assertNotIn('Fresh Chicken', payload['category_html'])
        self.assertIn('(1)', payload['category_html'])

    def test_selected_zero_count_category_survives_live_facet_refresh(self):
        other = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        Inventory.objects.create(
            product=self.sirloin, branch=other,
            quantity_on_hand=4, reorder_level=1,
        )

        response = self.client.get(reverse('orders:catalog'), {
            'branch': other.pk,
            'category': self.chicken.pk,
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        category_html = response.json()['category_html']
        self.assertIn('Fresh Chicken', category_html)
        self.assertIn('(0)', category_html)
        self.assertIn('checked', category_html)

    def test_category_selection_does_not_narrow_category_facet_counts(self):
        response = self.client.get(reverse('orders:catalog'), {
            'branch': self.branch.pk,
            'category': self.beef.pk,
        })

        counts = {
            category.category_name: category.stock_count
            for category in response.context['categories']
        }
        self.assertEqual(counts, {'Fresh Beef': 1, 'Fresh Chicken': 1, 'Rice & Grains': 1})
