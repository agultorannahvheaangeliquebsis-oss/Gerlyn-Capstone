"""Checkout says, explicitly, whether an order is delivered or collected.

F-11 — `delivery_address` was written straight from the POST with no
validation, and the form invited a blank one ("leave blank to pick up").
That made two different orders identical in the database:

  * a genuine pickup, and
  * a delivery whose address never arrived — a stripped form, a client bug,
    a mis-tap — which nobody downstream could tell was meant to be delivered.

`fulfillment_type` is now an explicit, required choice, and the address is
validated against it: Delivery must carry one, Pickup never does.

The choice also decides who may later apply the statutory PWD/Senior
discount (F-04 — staff can only sight an ID in person, so a Delivery order
never qualifies), which is why a blank or unrecognised value is refused
outright here rather than being defaulted to something.
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class CheckoutFulfillmentTypeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='ff-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-FF', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='ff-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 2}
        session.save()

    def check_out(self, **payload):
        payload.setdefault('mode_of_payment', 'Cash on Delivery')
        return self.client.post(
            reverse('orders:checkout'), payload, follow=True
        )

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    # ------------------------------------------------------------ delivery
    def test_delivery_without_an_address_is_refused_and_nothing_is_saved(self):
        response = self.check_out(
            fulfillment_type='Delivery', delivery_address='',
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(
            'Enter a delivery address, or choose Pickup instead — we cannot '
            'deliver an order without one.',
            self.messages(response),
        )
        # The cart survives the rejection — the shopper fixes one field and
        # resubmits, they do not start over.
        self.assertEqual(
            self.client.session['cart'], {str(self.product.pk): 2}
        )

    def test_a_whitespace_only_address_counts_as_blank(self):
        """'   ' is not an address. The check runs on the stripped value, so
        spaces cannot be used to satisfy a non-blank test."""
        response = self.check_out(
            fulfillment_type='Delivery', delivery_address='    ',
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(
            'Enter a delivery address, or choose Pickup instead — we cannot '
            'deliver an order without one.',
            self.messages(response),
        )

    def test_a_delivery_with_an_address_is_saved_as_a_delivery(self):
        self.check_out(
            fulfillment_type='Delivery',
            delivery_address='12 Real St, Baliuag',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(
            sale.fulfillment_type, SalesTransaction.FulfillmentType.DELIVERY
        )
        self.assertEqual(sale.delivery_address, '12 Real St, Baliuag')

    # -------------------------------------------------------------- pickup
    def test_pickup_with_a_blank_address_succeeds(self):
        """The case the old code could not distinguish from a lost address."""
        self.check_out(fulfillment_type='Pickup', delivery_address='')

        sale = SalesTransaction.objects.get()
        self.assertEqual(
            sale.fulfillment_type, SalesTransaction.FulfillmentType.PICKUP
        )
        self.assertEqual(sale.delivery_address, '')
        self.assertEqual(sale.order_status, 'Pending')

    def test_pickup_omitting_the_address_field_entirely_succeeds(self):
        """The browser does not submit a hidden field, so a real pickup POST
        has no delivery_address key at all — not merely an empty one."""
        self.check_out(fulfillment_type='Pickup')

        sale = SalesTransaction.objects.get()
        self.assertEqual(
            sale.fulfillment_type, SalesTransaction.FulfillmentType.PICKUP
        )
        self.assertEqual(sale.delivery_address, '')

    def test_a_stray_address_on_a_pickup_order_is_cleared_not_stored(self):
        """Typed an address, then switched to Pickup. Keeping the value would
        make the order read as a delivery on the console, the receipt and the
        customer's own tracking page, all of which key off this field."""
        self.check_out(
            fulfillment_type='Pickup',
            delivery_address='12 Real St, Baliuag',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(
            sale.fulfillment_type, SalesTransaction.FulfillmentType.PICKUP
        )
        self.assertEqual(sale.delivery_address, '')

    # ------------------------------------------------- missing / tampered
    def test_a_missing_fulfillment_type_is_refused_not_defaulted(self):
        """Unlike mode_of_payment and order_type, this has no safe default:
        guessing Delivery promises a delivery nobody asked for, and guessing
        Pickup strands a customer waiting at home. So it refuses."""
        response = self.check_out(delivery_address='12 Real St, Baliuag')

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(
            'Choose whether this order is for delivery or for pickup.',
            self.messages(response),
        )

    def test_an_unrecognised_fulfillment_type_is_refused(self):
        response = self.check_out(
            fulfillment_type='Teleport',
            delivery_address='12 Real St, Baliuag',
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(
            'Choose whether this order is for delivery or for pickup.',
            self.messages(response),
        )
