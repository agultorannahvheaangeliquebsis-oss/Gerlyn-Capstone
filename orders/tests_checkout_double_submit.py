"""Two near-simultaneous "Place Order" submits — a slow connection where the
first response hasn't landed when the shopper taps again, or a browser/
network retry of the same POST — used to create two separate orders from
one click, since checkout() had nothing checking whether a submission had
already been processed.

checkout_token (set when the cart page renders, rotated once consumed) plus
an atomic cache.add() closes this: of two requests carrying the same token,
only the first can ever win it.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class CheckoutDoubleSubmitTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='ds-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-DS', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='ds-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)

    def _load_cart_page(self):
        """The real path to a token: render the cart page, exactly like a
        shopper opening it in their browser before clicking Place Order."""
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 1}
        session.save()
        self.client.get(reverse('orders:cart'))
        return self.client.session['checkout_token']

    def test_two_submits_with_the_same_token_create_only_one_order(self):
        token = self._load_cart_page()
        payload = {
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Pickup',
            'checkout_token': token,
        }

        first = self.client.post(reverse('orders:checkout'), payload, follow=True)
        self.assertEqual(SalesTransaction.objects.count(), 1)

        # The test client is sequential, so by now the first POST has
        # already emptied the session cart — the pre-existing "cart is
        # empty" check would catch a second real request all on its own,
        # which is not what this guard is for and would pass even without
        # it. A genuine race is two requests that BOTH still see the
        # original, non-empty cart because neither has saved its session
        # yet — reinstating it here reproduces exactly that shape, isolating
        # the token check itself: same cart, same (already-consumed) token.
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()
        second = self.client.post(reverse('orders:checkout'), payload, follow=True)

        self.assertEqual(SalesTransaction.objects.count(), 1)
        self.assertContains(second, 'already be on its way')

    def test_a_missing_token_does_not_place_an_order(self):
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 1}
        session.save()
        # No prior GET to /cart/ — no token in the session at all, the shape
        # of a scripted or stale request rather than a real page submit.
        response = self.client.post(reverse('orders:checkout'), {
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Pickup',
        }, follow=True)

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertContains(response, 'already be on its way')

    def test_a_normal_single_submit_still_places_the_order(self):
        token = self._load_cart_page()
        response = self.client.post(reverse('orders:checkout'), {
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Pickup',
            'checkout_token': token,
        }, follow=True)

        self.assertEqual(SalesTransaction.objects.count(), 1)

    def test_a_failed_attempt_gets_a_fresh_token_for_the_retry(self):
        """A GCash order with no reference number is refused before any
        order is created — that must not burn the shopper's only token, or
        a legitimate retry after fixing the form would itself look like a
        replay."""
        token = self._load_cart_page()
        failed = self.client.post(reverse('orders:checkout'), {
            'mode_of_payment': 'GCash',
            'reference_number': '',
            'fulfillment_type': 'Pickup',
            'checkout_token': token,
        }, follow=True)
        self.assertEqual(SalesTransaction.objects.count(), 0)

        retry_token = self.client.session['checkout_token']
        self.assertNotEqual(retry_token, token)

        retry = self.client.post(reverse('orders:checkout'), {
            'mode_of_payment': 'GCash',
            'reference_number': 'GC-RETRY-1',
            'fulfillment_type': 'Pickup',
            'checkout_token': retry_token,
        }, follow=True)
        self.assertEqual(SalesTransaction.objects.count(), 1)
