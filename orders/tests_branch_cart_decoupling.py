"""The catalog's Branch checklist is a browse filter, not the shopping branch.

Storefront cart-integrity fix. The Filters rail's Branch checklist used to do
two jobs at once: filter the grid AND, when a single branch was ticked, call
_start_shopping_at() — which empties request.session['cart'] and queues a
Django message. On the live-filter (XHR) path only the grid HTML is swapped
in, so that message was never shown: ticking another branch to look at its
shelves silently threw the cart away.

Now the checklist only ever filters. The shopping branch is set solely by the
explicit controls (select_branch, the nav "Change" link, a card's "Choose a
branch" button). Consequences covered here:

  * ticking a branch — one or several, normal request or XHR — leaves the
    session branch and the cart untouched;
  * a cart with items but no branch can no longer be reached silently: the
    cart and checkout both redirect to branch selection with a message,
    rather than showing an empty cart while the header badge shows a count;
  * cart_count (the header badge) and _cart_items() agree in every reachable
    state — a cart with no branch counts as zero;
  * set_branch / clear_branch, which only _start_shopping_at()-on-GET served
    and nothing linked to, are gone.
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class BranchChecklistIsBrowseOnlyTests(TestCase):
    """Ticking the rail's Branch boxes filters the grid and nothing else."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='bc-owner', password='pw', role=User.Role.OWNER
        )
        self.branch_a = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.branch_b = Branch.objects.create(
            branch_name='Makinabang', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-BC', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('100.00'),
        )
        for branch in (self.branch_a, self.branch_b):
            Inventory.objects.create(
                product=self.product, branch=branch,
                quantity_on_hand=50, reorder_level=5,
            )
        self.shopper = User.objects.create_user(
            username='bc-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)

        session = self.client.session
        session['branch_id'] = self.branch_a.pk
        session['cart'] = {str(self.product.pk): 2}
        session.save()

    def test_ticking_one_branch_filters_without_touching_session_or_cart(self):
        response = self.client.get(
            reverse('orders:catalog'), {'branch': self.branch_b.pk}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session['branch_id'], self.branch_a.pk)
        self.assertEqual(
            self.client.session['cart'], {str(self.product.pk): 2}
        )

    def test_same_over_the_live_filter_xhr_path(self):
        response = self.client.get(
            reverse('orders:catalog'), {'branch': self.branch_b.pk},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(self.client.session['branch_id'], self.branch_a.pk)
        self.assertEqual(
            self.client.session['cart'], {str(self.product.pk): 2}
        )

    def test_ticking_several_branches_does_not_clear_the_session_branch(self):
        """The old code popped branch_id and emptied the cart the moment the
        tick count was anything other than exactly one."""
        response = self.client.get(
            reverse('orders:catalog'),
            {'branch': [self.branch_a.pk, self.branch_b.pk]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session['branch_id'], self.branch_a.pk)
        self.assertEqual(
            self.client.session['cart'], {str(self.product.pk): 2}
        )

    def test_summary_branch_follows_the_filter_not_the_session_branch(self):
        response = self.client.get(
            reverse('orders:catalog'), {'branch': self.branch_b.pk}
        )
        # The "N of M items at <branch>" line names the branch being browsed,
        # which is no longer the same as the cart's shopping branch.
        self.assertEqual(response.context['summary_branch'], self.branch_b)
        self.assertEqual(response.context['selected_branch_id'], self.branch_a.pk)


class CartRequiresABranchTests(TestCase):
    """A cart with items but no branch redirects to branch selection."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='cr-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-CR', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('100.00'),
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='cr-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)

    def _stale_cart_no_branch(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 2}
        session.pop('branch_id', None)
        session.save()

    def test_cart_page_with_items_and_no_branch_redirects_to_select_branch(self):
        self._stale_cart_no_branch()

        response = self.client.get(reverse('orders:cart'))

        self.assertRedirects(response, reverse('orders:select_branch'))
        self.assertIn('Pick a branch to check out.', _messages(response))

    def test_checkout_with_items_and_no_branch_redirects_and_writes_nothing(self):
        self._stale_cart_no_branch()

        response = self.client.post(
            reverse('orders:checkout'),
            {'mode_of_payment': 'Cash on Delivery', 'fulfillment_type': 'Pickup'},
            follow=True,
        )

        self.assertEqual(
            response.redirect_chain[-1][0], reverse('orders:select_branch')
        )
        self.assertIn('Pick a branch to check out.', _messages(response))
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_empty_cart_and_no_branch_still_shows_the_normal_empty_cart(self):
        """No items means nothing to strand — the cart page renders its empty
        state rather than bouncing to branch selection."""
        session = self.client.session
        session['cart'] = {}
        session.pop('branch_id', None)
        session.save()

        response = self.client.get(reverse('orders:cart'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['items']), 0)

    def test_cart_page_with_a_branch_set_renders_normally(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 2}
        session['branch_id'] = self.branch.pk
        session.save()

        response = self.client.get(reverse('orders:cart'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['items']), 1)


class CartCountMatchesCartItemsTests(TestCase):
    """The header badge never disagrees with what the cart page will show."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='cc-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.p1 = Product.objects.create(
            sku_code='SKU-CC1', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('100.00'),
        )
        self.p2 = Product.objects.create(
            sku_code='SKU-CC2', product_name='Cooking Oil (1L)',
            unit_of_measure='Bottle', unit_price=Decimal('80.00'),
        )
        for product in (self.p1, self.p2):
            Inventory.objects.create(
                product=product, branch=self.branch,
                quantity_on_hand=50, reorder_level=5,
            )
        self.shopper = User.objects.create_user(
            username='cc-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)

    def test_no_branch_and_no_cart_reads_zero(self):
        # "/" (home) takes cart_count straight from the customer_nav context
        # processor — the shared source for every storefront header.
        response = self.client.get('/')
        self.assertEqual(response.context['cart_count'], 0)

    def test_stale_cart_with_no_branch_reads_zero_not_the_raw_length(self):
        session = self.client.session
        session['cart'] = {str(self.p1.pk): 2, str(self.p2.pk): 1}
        session.pop('branch_id', None)
        session.save()

        # The badge is honest even though session['cart'] still has two keys —
        # the cart page would redirect this shopper to pick a branch.
        response = self.client.get('/')
        self.assertEqual(response.context['cart_count'], 0)

    def test_with_a_branch_the_badge_matches_the_rendered_cart(self):
        session = self.client.session
        session['cart'] = {str(self.p1.pk): 2, str(self.p2.pk): 1}
        session['branch_id'] = self.branch.pk
        session.save()

        response = self.client.get(reverse('orders:cart'))

        self.assertEqual(len(response.context['items']), 2)
        self.assertEqual(response.context['cart_count'], 2)


class RemovedBranchRoutesTests(TestCase):
    """set_branch and clear_branch are gone — routes and reverses both."""

    def test_set_branch_reverse_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse('orders:set_branch', args=[1])

    def test_clear_branch_reverse_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse('orders:clear_branch')

    def test_the_old_paths_now_404(self):
        self.assertEqual(self.client.get('/shop/branch/1/').status_code, 404)
        self.assertEqual(self.client.get('/shop/branch/clear/').status_code, 404)

    def test_select_branch_itself_still_resolves(self):
        self.assertEqual(
            reverse('orders:select_branch'), '/shop/branch/'
        )
