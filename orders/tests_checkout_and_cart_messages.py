"""Storefront loose ends: every exit speaks, and GCash carries its reference.

Four audit findings, all in orders.customer_views, all of the same family —
a code path that changed something (or refused to) and then said nothing, or
wrote a field nobody had checked:

  * L8 — checkout() accepted a GCash order with no reference number, and
    wrote request.POST['reference_number'] onto the billing row whatever the
    payment method was. No gateway is integrated, so for GCash that string is
    the only record of the transfer; for every other method it is noise on a
    row that has no reference by definition.
  * L6 — checkout() on GET bounced to the cart with no message, and
    update_cart's "decrease" changed the number in silence while a decrease
    at the floor (qty 1) did nothing at all and still said nothing.
  * L7 — undo_add redirected to a raw, unvalidated HTTP_REFERER while every
    other redirect in the file goes through _safe_next().
  * L4 — my_orders ran five .count() queries into a `counts` context key
    that my_orders.html never reads (its tabs are plain text by design).
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class StorefrontShopperTestCase(TestCase):
    """A logged-in customer with a branch chosen and two sacks in the cart."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='lo-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-LO', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='lo-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 2}
        # Matched against the POST's own checkout_token by checkout()'s
        # double-submit guard — real traffic always has one from rendering
        # the cart page first, which writing the session directly skips.
        session['checkout_token'] = 'test-checkout-token'
        session.save()


# ─────────────────────────────────────────────────────────────────────────
# L8 — GCash reference number
# ─────────────────────────────────────────────────────────────────────────
class GCashReferenceTests(StorefrontShopperTestCase):
    """GCash needs a reference; nothing else may keep one."""

    def check_out(self, **payload):
        payload.setdefault('fulfillment_type', 'Pickup')
        payload.setdefault('checkout_token', 'test-checkout-token')
        return self.client.post(reverse('orders:checkout'), payload, follow=True)

    def test_gcash_with_no_reference_is_refused_and_nothing_is_saved(self):
        response = self.check_out(mode_of_payment='GCash', reference_number='')

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(
            'Enter your GCash reference number, or choose another payment '
            'method — we cannot match the payment to your order without it.',
            _messages(response),
        )
        # The cart survives the rejection: one field to fix, not a restart.
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 2})

    def test_gcash_with_a_missing_reference_field_is_refused(self):
        """A stripped form is the same as a blank box, not a pass."""
        response = self.check_out(mode_of_payment='GCash')

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(
            'Enter your GCash reference number, or choose another payment '
            'method — we cannot match the payment to your order without it.',
            _messages(response),
        )

    def test_a_whitespace_only_reference_counts_as_blank(self):
        response = self.check_out(
            mode_of_payment='GCash', reference_number='   '
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_gcash_with_a_reference_is_accepted_and_records_it(self):
        response = self.check_out(
            mode_of_payment='GCash', reference_number=' GC-2026-0001 '
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.mode_of_payment, 'GCash')
        # Stored stripped, the same treatment delivery_address gets.
        self.assertEqual(sale.billing_record.reference_number, 'GC-2026-0001')
        self.assertEqual(sale.billing_record.payment_method, 'GCash')

    def test_a_non_gcash_order_does_not_keep_a_typed_reference(self):
        """The box is always on the form. A reference typed and then abandoned
        for Cash on Delivery used to ride along onto the billing row."""
        self.check_out(
            mode_of_payment='Cash on Delivery',
            reference_number='GC-2026-0001',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.mode_of_payment, 'Cash on Delivery')
        self.assertEqual(sale.billing_record.reference_number, '')

    def test_an_unrecognised_method_falls_back_to_cod_and_clears_the_reference(self):
        """mode_of_payment falls back to COD when it is not a real choice —
        the reference must be cleared against the METHOD THAT IS STORED, not
        the one that was posted, or a bogus method smuggles a reference in."""
        self.check_out(
            mode_of_payment='Bitcoin', reference_number='GC-2026-0001'
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.mode_of_payment, 'Cash on Delivery')
        self.assertEqual(sale.billing_record.reference_number, '')


# ─────────────────────────────────────────────────────────────────────────
# L6 — state exits that used to say nothing
# ─────────────────────────────────────────────────────────────────────────
class CheckoutGetBounceTests(StorefrontShopperTestCase):
    """There is no checkout page, so a GET explains where the form is."""

    def test_get_checkout_redirects_to_the_cart_with_a_message(self):
        response = self.client.get(reverse('orders:checkout'), follow=True)

        self.assertEqual(response.redirect_chain[-1][0], reverse('orders:cart'))
        self.assertIn(
            'Review your order below, then press Place Order.',
            _messages(response),
        )
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_the_empty_cart_message_still_wins_over_the_get_bounce(self):
        """An empty cart is answered before the method check — being told to
        press Place Order on a cart with nothing in it would be nonsense."""
        session = self.client.session
        session['cart'] = {}
        session.save()

        response = self.client.get(reverse('orders:checkout'), follow=True)

        self.assertIn('Your cart is empty.', _messages(response))
        self.assertNotIn(
            'Review your order below, then press Place Order.',
            _messages(response),
        )


class UpdateCartMessagesTests(StorefrontShopperTestCase):
    """Every + and − says what it did, including the one that does nothing."""

    def bump(self, action):
        return self.client.post(
            reverse('orders:update_cart', args=[self.product.pk]),
            {'action': action}, follow=True,
        )

    def test_decrease_confirms_the_new_quantity(self):
        response = self.bump('decrease')

        self.assertEqual(self.client.session['cart'][str(self.product.pk)], 1)
        self.assertIn('Rice (Sack) — 1 in your cart.', _messages(response))

    def test_increase_confirms_the_new_quantity(self):
        response = self.bump('increase')

        self.assertEqual(self.client.session['cart'][str(self.product.pk)], 3)
        self.assertIn('Rice (Sack) — 3 in your cart.', _messages(response))

    def test_decrease_at_the_floor_says_so_instead_of_doing_nothing(self):
        session = self.client.session
        session['cart'] = {str(self.product.pk): 1}
        session.save()

        response = self.bump('decrease')

        self.assertEqual(self.client.session['cart'][str(self.product.pk)], 1)
        self.assertIn(
            'Rice (Sack) is down to one. Use Remove to take it out of your cart.',
            _messages(response),
        )

    def test_an_unknown_action_changes_nothing_and_says_so(self):
        response = self.bump('teleport')

        self.assertEqual(self.client.session['cart'][str(self.product.pk)], 2)
        self.assertIn('That cart change was not understood.', _messages(response))

    def test_increase_past_available_stock_still_refuses(self):
        """The pre-existing refusal keeps its wording — the new success
        messages sit beside it, they do not replace it."""
        session = self.client.session
        session['cart'] = {str(self.product.pk): 50}
        session.save()

        response = self.bump('increase')

        self.assertEqual(self.client.session['cart'][str(self.product.pk)], 50)
        self.assertIn(
            'Only 50 of Rice (Sack) left at this branch.', _messages(response)
        )


# ─────────────────────────────────────────────────────────────────────────
# L7 — undo_add's redirect target is validated
# ─────────────────────────────────────────────────────────────────────────
class UndoAddRedirectTests(StorefrontShopperTestCase):
    """The Referer decides where Undo lands, but only if it is ours."""

    def setUp(self):
        super().setUp()
        session = self.client.session
        session['last_added'] = {'id': str(self.product.pk), 'qty': 2}
        session.save()

    def undo(self, **extra):
        # follow=False: an off-site Location must not actually be followed.
        return self.client.post(reverse('orders:undo_add'), **extra)

    def test_an_offsite_referer_is_ignored_in_favour_of_the_catalog(self):
        response = self.undo(HTTP_REFERER='https://evil.example.com/harvest')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('orders:catalog'))

    def test_a_scheme_relative_referer_is_ignored_too(self):
        """//evil.example.com is an absolute URL to a browser, and reads as a
        path to anything doing a naive "does it start with /" check."""
        response = self.undo(HTTP_REFERER='//evil.example.com/harvest')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('orders:catalog'))

    def test_an_onsite_referer_is_honoured(self):
        back = reverse('orders:product_detail', args=[self.product.pk])
        response = self.undo(HTTP_REFERER=back)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], back)

    def test_no_referer_at_all_falls_back_to_the_catalog(self):
        response = self.undo()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('orders:catalog'))

    def test_the_undo_itself_still_works(self):
        """Guard against fixing the redirect and breaking the point of it."""
        self.undo(HTTP_REFERER=reverse('orders:catalog'))

        self.assertEqual(self.client.session['cart'], {})

    def test_the_ajax_path_is_untouched_by_the_redirect_change(self):
        response = self.client.post(
            reverse('orders:undo_add'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_REFERER='https://evil.example.com/harvest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {'ok': True, 'cart_count': 0})


# ─────────────────────────────────────────────────────────────────────────
# L4 — the dead counts queries
# ─────────────────────────────────────────────────────────────────────────
class MyOrdersHasNoDeadCountsTests(StorefrontShopperTestCase):
    """my_orders renders its tabs without counting anything for them."""

    def setUp(self):
        super().setUp()
        for status in ('Pending', 'Approved', 'Preparing', 'Completed', 'Cancelled'):
            SalesTransaction.objects.create(
                branch=self.branch, customer=self.shopper, staff=None,
                transaction_type=SalesTransaction.TransactionType.ONLINE,
                order_status=status,
                fulfillment_type=SalesTransaction.FulfillmentType.PICKUP,
            )

    def test_the_counts_context_key_is_gone(self):
        response = self.client.get(reverse('orders:my_orders'))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('counts', response.context)

    def test_the_tabs_still_filter(self):
        """The table `counts` was built from still drives the filtering, which
        is the half of it that was ever used."""
        response = self.client.get(
            reverse('orders:my_orders'), {'status': 'cancelled'}
        )

        self.assertEqual(response.status_code, 200)
        rows = list(response.context['orders'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].order_status, 'Cancelled')

    def test_an_unknown_status_still_falls_back_to_all(self):
        response = self.client.get(
            reverse('orders:my_orders'), {'status': 'nonsense'}
        )

        self.assertEqual(response.context['status_filter'], '')
        self.assertEqual(len(list(response.context['orders'])), 5)
