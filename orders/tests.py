"""add_to_cart takes its quantity straight from POST.

F-08c / F-09 — the int() cast on that raw value used to be unguarded:
  - a non-numeric quantity ("abc") raised ValueError and 500ed.
  - a negative quantity parsed fine, then passed the `already + qty >
    available` stock check trivially and *shrank* an existing cart line
    instead of being rejected.

Both are fixed at the one cast site: non-numeric falls back to 1, and
anything below 1 is refused before it reaches the stock comparison. The
last test here is the regression guard — a normal positive quantity must
still behave exactly as it did before.
"""
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product


class AddToCartQuantityGuardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='cart-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Cart Branch', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-CART', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=100,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.id
        session.save()

        self.url = reverse('orders:add_to_cart', args=[self.product.pk])

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_non_numeric_quantity_falls_back_to_one(self):
        response = self.client.post(self.url, {'quantity': 'abc'})
        # No 500 — the add goes through at the fallback quantity of 1.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 1})

    def test_a_negative_quantity_is_rejected_not_silently_applied(self):
        # An existing line the negative value would otherwise shrink.
        session = self.client.session
        session['cart'] = {str(self.product.pk): 5}
        session.save()

        response = self.client.post(self.url, {'quantity': '-3'})

        self.assertIn('Enter a quantity of at least 1.', self._messages(response))
        # The line is untouched — not 5 + (-3) = 2.
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 5})

    def test_a_zero_quantity_is_rejected_too(self):
        response = self.client.post(self.url, {'quantity': '0'})
        self.assertIn('Enter a quantity of at least 1.', self._messages(response))
        self.assertNotIn('cart', self.client.session)

    def test_a_negative_quantity_over_ajax_is_a_409_not_a_500(self):
        response = self.client.post(
            self.url, {'quantity': '-1'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('cart', self.client.session)

    def test_a_normal_positive_quantity_still_adds_exactly_that_many(self):
        """Regression guard — same spirit as F-05's 'valid GCash still kept
        as GCash'. The guard must not change the happy path."""
        response = self.client.post(self.url, {'quantity': '3'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 3})

        # A second add stacks on the first, exactly as before.
        self.client.post(self.url, {'quantity': '2'})
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 5})
