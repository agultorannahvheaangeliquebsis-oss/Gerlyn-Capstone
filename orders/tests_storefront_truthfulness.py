"""The storefront says only what the system can actually do.

A copy-and-scope pass over the shop's public pages, after an audit found six
places where a page stated something the code behind it does not deliver.
The ones with behaviour behind them are covered here; the rest are wording
changes in about.html, cart.html and home_content.html with no branch to
assert on.

  * The product page's stock chip AND its Add to Cart button both read
    product.on_hand, which was summed across every branch while the cart
    only ever accepts stock from the session's branch. A shopper at one
    branch was shown "In stock" for something only another branch had, given
    a live button, and refused by add_to_cart. on_hand is now branch-scoped.

  * The catalog's Category filter rail passed its own unconditional
    ProductCategory.objects.all(), ignoring the fewer-than-two threshold the
    rest of the storefront keys its category UI off — so a one-category store
    hid the header chips and kept a rail offering the single tick-box that
    filters nothing.

  * The confirmation page printed SalesTransaction.order_status raw, so the
    order a shopper had just placed read "Pending" there and "Waiting for
    the store" on My Orders one click later. Both now render the same
    partial, and the page has a way back into My Orders at all.

  * view_cart's `qualifies` guarded `total >= 500 and total > 0`, whose
    second half cannot be false when the first is true.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product, ProductCategory
from sales.models import SalesTransaction


class ProductDetailStockIsBranchScopedTests(TestCase):
    """The PDP chip and CTA count the shelf the cart will actually draw from."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='sf-owner', password='pw', role=User.Role.OWNER
        )
        self.here = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.elsewhere = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-SF1', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('100.00'),
        )
        # Stocked at Makinabang only. Nothing on the Tarcan shelf at all —
        # no Inventory row, which is the harsher of the two ways a branch can
        # be out (the other being a row at zero).
        Inventory.objects.create(
            product=self.product, branch=self.elsewhere,
            quantity_on_hand=40, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='sf-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)

    def _shop_at(self, branch):
        session = self.client.session
        session['branch_id'] = branch.pk
        session.save()

    def test_stock_is_zero_when_the_chosen_branch_has_none(self):
        self._shop_at(self.here)

        response = self.client.get(
            reverse('orders:product_detail', args=[self.product.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['product'].on_hand, 0)
        # And the panel names the branch rather than implying the whole store
        # is out, since the breakdown below shows Makinabang holding 40.
        self.assertContains(response, 'Out of stock at Tarcan')

    def test_stock_is_the_branch_figure_when_the_chosen_branch_has_some(self):
        self._shop_at(self.elsewhere)

        response = self.client.get(
            reverse('orders:product_detail', args=[self.product.pk])
        )

        self.assertEqual(response.context['product'].on_hand, 40)
        self.assertEqual(response.context['selected_branch'], self.elsewhere)

    def test_with_no_branch_chosen_the_figure_is_the_whole_store(self):
        """"Available somewhere" is the honest reading before a branch is
        picked — and the panel is offering "Choose a branch to buy", not a
        live Add to Cart, so nothing is promised that the cart would refuse."""
        response = self.client.get(
            reverse('orders:product_detail', args=[self.product.pk])
        )

        self.assertEqual(response.context['product'].on_hand, 40)
        self.assertIsNone(response.context['selected_branch'])
        self.assertContains(response, 'Choose a branch to buy')

    def test_the_per_branch_panel_still_shows_every_branch(self):
        """Scoping the chip must not narrow the breakdown underneath it."""
        self._shop_at(self.here)

        response = self.client.get(
            reverse('orders:product_detail', args=[self.product.pk])
        )

        rows = {
            row['branch'].branch_name: row['qty']
            for row in response.context['branch_stock']
        }
        self.assertEqual(rows, {'Tarcan': 0, 'Makinabang': 40})


class CatalogCategoryRailThresholdTests(TestCase):
    """The filter rail obeys the same fewer-than-two rule as the header."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='sf-cat-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.general = ProductCategory.objects.create(category_name='General')
        product = Product.objects.create(
            sku_code='SKU-SF2', product_name='Soap', category=self.general,
            unit_of_measure='Piece', unit_price=Decimal('20.00'),
        )
        Inventory.objects.create(
            product=product, branch=self.branch,
            quantity_on_hand=10, reorder_level=2,
        )

    def test_one_category_draws_no_rail(self):
        response = self.client.get(reverse('orders:catalog'))

        self.assertEqual(list(response.context['categories']), [])
        self.assertNotContains(response, 'rail-title">Category')

    def test_a_second_category_brings_the_rail_back(self):
        # Stocked, not just created. An empty category is not drawn at all —
        # see storefront_categories — so a bare create() here would leave the
        # store on one drawable category and the rail correctly still hidden.
        rice = ProductCategory.objects.create(category_name='Rice')
        grain = Product.objects.create(
            sku_code='SKU-SF2-RICE', product_name='Rice 5kg', category=rice,
            unit_of_measure='Sack', unit_price=Decimal('280.00'),
        )
        Inventory.objects.create(
            product=grain, branch=self.branch,
            quantity_on_hand=4, reorder_level=1,
        )

        response = self.client.get(reverse('orders:catalog'))

        self.assertEqual(len(response.context['categories']), 2)
        self.assertContains(response, 'rail-title">Category')

    def test_an_empty_second_category_is_not_drawn(self):
        """A category with nothing in it is a link to an empty grid."""
        ProductCategory.objects.create(category_name='Rice')

        response = self.client.get(reverse('orders:catalog'))

        self.assertEqual(list(response.context['categories']), [])
        self.assertNotContains(response, 'rail-title">Category')


class ConfirmationPageTests(TestCase):
    """What the shopper is shown the moment their order is placed."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='sf-conf-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.shopper = User.objects.create_user(
            username='sf-conf-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)
        self.sale = SalesTransaction.objects.create(
            branch=self.branch, customer=self.shopper,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PENDING,
            mode_of_payment=SalesTransaction.ModeOfPayment.COD,
            fulfillment_type=SalesTransaction.FulfillmentType.PICKUP,
        )

    def test_status_is_worded_for_the_customer_not_the_raw_enum(self):
        response = self.client.get(
            reverse('orders:confirmation', args=[self.sale.pk])
        )

        self.assertContains(response, 'Waiting for the store')
        self.assertNotContains(response, '>Pending<')

    def test_the_same_wording_appears_on_my_orders(self):
        """The two pages read the same order one click apart, so they share
        one partial rather than two lists of {% if %} branches."""
        confirmation = self.client.get(
            reverse('orders:confirmation', args=[self.sale.pk])
        )
        my_orders = self.client.get(reverse('orders:my_orders'))

        self.assertContains(confirmation, 'Waiting for the store')
        self.assertContains(my_orders, 'Waiting for the store')

    def test_there_is_a_way_back_into_my_orders(self):
        response = self.client.get(
            reverse('orders:confirmation', args=[self.sale.pk])
        )

        self.assertContains(response, reverse('orders:my_orders'))
        self.assertContains(response, 'breadcrumb')


class FreeDeliveryThresholdTests(TestCase):
    """`qualifies` at, above and below the threshold, and on an empty cart."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='sf-fd-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-SF3', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('250.00'),
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=40, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='sf-fd-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)

    def _cart(self, quantity):
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): quantity} if quantity else {}
        session.save()
        return self.client.get(reverse('orders:cart'))

    def test_an_empty_cart_does_not_qualify(self):
        """The dropped `and total > 0` guarded this case and nothing else —
        it has to keep holding without it."""
        response = self._cart(0)

        self.assertFalse(response.context['qualifies'])
        self.assertEqual(response.context['progress_pct'], 0)

    def test_below_the_threshold_does_not_qualify(self):
        response = self._cart(1)

        self.assertFalse(response.context['qualifies'])
        self.assertEqual(response.context['remaining_for_free'], Decimal('250.00'))

    def test_exactly_the_threshold_qualifies(self):
        response = self._cart(2)

        self.assertTrue(response.context['qualifies'])
        self.assertEqual(response.context['remaining_for_free'], Decimal('0'))

    def test_below_the_threshold_promises_no_quote_it_cannot_produce(self):
        """checkout() records no delivery fee, so the cart must not say one
        is coming — the total shown is the total written to the order."""
        response = self._cart(1)

        self.assertNotContains(response, 'Calculated at delivery')
        self.assertContains(response, 'Arranged with the branch')
