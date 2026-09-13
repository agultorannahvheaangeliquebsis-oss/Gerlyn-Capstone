"""A product deleted (or its Inventory row at the branch removed) while it
sits in a shopper's session cart used to be a permanent checkout blocker.

_cart_items already skipped a line like this from the cart page's display —
but only from the display. checkout() read request.session['cart'] directly,
saw the same line resolve to 0 available, and refused the WHOLE order with
"only 0 left" — forever, since the shopper could no longer see the line
anywhere on the page to remove it. The only way out was switching branches,
which wipes every other item in the cart too.

Both _cart_items (the cart page) and checkout() now purge a line like this
from the session cart itself the moment they can't resolve it, so it can
never block a later checkout attempt and the rest of the cart survives
intact.
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class OrphanedCartItemTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='oc-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.good_product = Product.objects.create(
            sku_code='SKU-OC-GOOD', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
        )
        Inventory.objects.create(
            product=self.good_product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        # Carried in the cart, then its Inventory row (and the product
        # itself) is removed — the same end state whether staff delete the
        # product outright or just pull it from this branch's floor stock.
        self.gone_product = Product.objects.create(
            sku_code='SKU-OC-GONE', product_name='Discontinued Snack',
            unit_of_measure='Pack', unit_price=Decimal('20.00'),
        )
        self.shopper = User.objects.create_user(
            username='oc-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {
            str(self.good_product.pk): 1,
            str(self.gone_product.pk): 2,
        }
        session.save()

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_cart_page_drops_the_orphaned_line_from_the_session(self):
        response = self.client.get(reverse('orders:cart'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.session['cart'], {str(self.good_product.pk): 1}
        )

    def test_checkout_succeeds_for_the_remaining_items(self):
        """The old bug: checkout refused the entire order forever over a
        line the shopper could not even see to remove. Now it purges that
        line and places the order for what is actually left."""
        response = self.client.post(
            reverse('orders:checkout'),
            {'mode_of_payment': 'Cash on Delivery', 'fulfillment_type': 'Pickup'},
            follow=True,
        )

        self.assertEqual(SalesTransaction.objects.count(), 0, (
            'the first POST purges the stale line and bounces back to the '
            'cart — it does not place a partial order silently'
        ))
        self.assertEqual(
            self.client.session['cart'], {str(self.good_product.pk): 1}
        )

        # Second attempt: the cart is now clean, so checkout goes through.
        response = self.client.post(
            reverse('orders:checkout'),
            {'mode_of_payment': 'Cash on Delivery', 'fulfillment_type': 'Pickup'},
            follow=True,
        )

        self.assertEqual(SalesTransaction.objects.count(), 1)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.items.count(), 1)
        self.assertEqual(sale.items.get().product_id, self.good_product.pk)
        self.assertEqual(self.client.session['cart'], {})

    def test_checkout_never_gets_permanently_stuck(self):
        """Before the fix, this exact sequence looped forever: the stale
        line reappeared under the same session key on every attempt because
        nothing ever removed it from request.session['cart']."""
        first = self.client.post(
            reverse('orders:checkout'),
            {'mode_of_payment': 'Cash on Delivery', 'fulfillment_type': 'Pickup'},
            follow=True,
        )
        self.assertNotIn(str(self.gone_product.pk), self.client.session['cart'])

        second = self.client.post(
            reverse('orders:checkout'),
            {'mode_of_payment': 'Cash on Delivery', 'fulfillment_type': 'Pickup'},
            follow=True,
        )
        self.assertEqual(SalesTransaction.objects.count(), 1)
