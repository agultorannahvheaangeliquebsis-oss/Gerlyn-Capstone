"""The product page's "Recently viewed" rail.

It is built in customer_views.product_detail from a short list of product ids
kept in the session, newest first, with the product on screen left out of
its own rail and anything no longer buyable filtered out — the same in-stock
gate the "You may also like" rail uses.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product


class RecentlyViewedRailTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='rv-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.shopper = User.objects.create_user(
            username='rv-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)

        self.a, self.b, self.c = (
            self._product('A'), self._product('B'), self._product('C')
        )

    def _product(self, tag, stock=25):
        p = Product.objects.create(
            sku_code=f'SKU-RV{tag}', product_name=f'Item {tag}',
            unit_of_measure='Piece', unit_price=Decimal('10.00'),
        )
        if stock:
            Inventory.objects.create(
                product=p, branch=self.branch,
                quantity_on_hand=stock, reorder_level=5,
            )
        return p

    def _view(self, product):
        return self.client.get(
            reverse('orders:product_detail', args=[product.pk])
        )

    def test_first_visit_has_an_empty_rail_but_records_the_product(self):
        response = self._view(self.a)

        self.assertEqual(list(response.context['recently_viewed']), [])
        self.assertEqual(self.client.session['recently_viewed'], [self.a.pk])

    def test_rail_shows_earlier_products_newest_first(self):
        self._view(self.a)
        self._view(self.b)
        response = self._view(self.c)

        self.assertEqual(
            [p.pk for p in response.context['recently_viewed']],
            [self.b.pk, self.a.pk],
        )

    def test_product_is_left_out_of_its_own_rail(self):
        self._view(self.a)
        self._view(self.b)
        response = self._view(self.a)

        self.assertEqual(
            [p.pk for p in response.context['recently_viewed']], [self.b.pk]
        )
        # And it moves back to the head of the stored list.
        self.assertEqual(
            self.client.session['recently_viewed'], [self.a.pk, self.b.pk]
        )

    def test_a_product_that_went_out_of_stock_drops_off_the_rail(self):
        self._view(self.a)
        Inventory.objects.filter(product=self.a).update(quantity_on_hand=0)

        response = self._view(self.b)

        self.assertEqual(list(response.context['recently_viewed']), [])
        # Still remembered — it returns to the rail if it is restocked.
        self.assertIn(self.a.pk, self.client.session['recently_viewed'])

    def test_stored_list_is_capped(self):
        products = [self._product(f'X{i}') for i in range(15)]
        for p in products:
            self._view(p)

        self.assertEqual(len(self.client.session['recently_viewed']), 12)
        # The most recent visit is at the head.
        self.assertEqual(
            self.client.session['recently_viewed'][0], products[-1].pk
        )
