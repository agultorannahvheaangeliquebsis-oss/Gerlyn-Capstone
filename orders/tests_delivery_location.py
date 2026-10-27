"""Location is optional at checkout; a delivery still needs somewhere to go.

What these pin down (orders.delivery, called by checkout and the sales API):

  * Declining to share a location ("Don't share my location") stores no
    coordinates, even if the form still carries ones captured earlier or the
    saved address has a pin.
  * The browser refusing (denied, timed out, unsupported) is handled the same
    way and never blocks the order.
  * A Delivery order needs a pinned location OR a complete typed address
    (street, barangay, municipality). A Pickup order needs neither and carries
    nothing location-related at all.
  * A pin that is kept is rounded to four decimal places.
  * The typed address can be saved to the account; the pin never is.
"""
import itertools
from decimal import Decimal

from django.contrib.messages import get_messages
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from accounts.models import Address, User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction

from .delivery import INCOMPLETE_ADDRESS, NEEDS_CONTACT

CONTACT = '0917 123 4567'
TYPED = {
    'address_source': 'typed',
    'addr_street': '14 Rizal St.',
    'addr_barangay': 'Poblacion',
    'addr_city': 'Baliuag',
    'addr_contact': CONTACT,
}


class DeliveryCheckoutBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='dl-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-DL', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='dl-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.shopper)
        self.fill_cart()

    _tokens = itertools.count(1)

    def fill_cart(self):
        """A full cart and a FRESH checkout token.

        checkout() burns the token it is given (a cache.add guard against
        double-submits) even when it goes on to refuse the order, so every
        attempt in a test needs its own.
        """
        self.token = f'dl-token-{next(self._tokens)}'
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 2}
        session['checkout_token'] = self.token
        session.save()

    def set_contact_on_file(self, number):
        # Creating a customer already creates their (empty) profile.
        profile = self.shopper.customer_profile
        profile.contact_number = number
        profile.save()

    def save_address(self, text='12 Real St, Baliuag', **extra):
        return Address.objects.create(
            user=self.shopper, is_default=True, text=text, **extra
        )

    def check_out(self, **payload):
        self.fill_cart()
        payload.setdefault('mode_of_payment', 'Cash on Delivery')
        payload.setdefault('checkout_token', self.token)
        payload.setdefault('fulfillment_type', 'Delivery')
        return self.client.post(reverse('orders:checkout'), payload, follow=True)

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class RejectingLocationTests(DeliveryCheckoutBase):
    """"Don't share my location" — the customer says no."""

    def test_declining_stores_no_coordinates_and_the_order_goes_through(self):
        self.save_address()

        self.check_out(addr_contact=CONTACT, location_status='declined')

        sale = SalesTransaction.objects.get()
        self.assertIsNone(sale.delivery_latitude)
        self.assertIsNone(sale.delivery_longitude)
        self.assertFalse(sale.has_delivery_pin)
        self.assertEqual(sale.delivery_address, '12 Real St, Baliuag')

    def test_declining_clears_coordinates_captured_earlier_in_the_form(self):
        """The shopper pressed "Use my current location", then changed their
        mind and pressed "Don't share". The browser script blanks the hidden
        fields, but even a stale POST that still carries them is ignored."""
        self.save_address()

        self.check_out(
            addr_contact=CONTACT, location_status='declined',
            delivery_latitude='14.951234', delivery_longitude='120.900567',
        )

        sale = SalesTransaction.objects.get()
        self.assertIsNone(sale.delivery_latitude)
        self.assertIsNone(sale.delivery_longitude)

    def test_declining_leaves_out_the_pin_saved_on_the_address(self):
        self.save_address(latitude=Decimal('14.951234'), longitude=Decimal('120.900567'))

        self.check_out(addr_contact=CONTACT, location_status='declined')

        sale = SalesTransaction.objects.get()
        self.assertFalse(sale.has_delivery_pin)

    def test_a_typed_address_alone_is_enough_when_location_is_declined(self):
        self.check_out(**TYPED, location_status='declined')

        sale = SalesTransaction.objects.get()
        self.assertFalse(sale.has_delivery_pin)
        self.assertEqual(sale.delivery_address, '14 Rizal St., Brgy. Poblacion, Baliuag')
        self.assertEqual(sale.delivery_contact_number, CONTACT)

    def test_coordinates_are_never_read_without_a_granted_status(self):
        """Hidden fields filled in, but the form never said "shared"."""
        self.check_out(
            **TYPED, delivery_latitude='14.951234', delivery_longitude='120.900567',
        )

        self.assertFalse(SalesTransaction.objects.get().has_delivery_pin)


class BrowserDeniesLocationTests(DeliveryCheckoutBase):
    """Permission denied, timed out, or no geolocation: same fallback."""

    def test_a_denied_browser_never_blocks_a_delivery_with_a_typed_address(self):
        response = self.check_out(
            **TYPED, location_status='denied',
            delivery_latitude='', delivery_longitude='',
        )

        sale = SalesTransaction.objects.get()
        self.assertFalse(sale.has_delivery_pin)
        self.assertEqual(sale.delivery_address, '14 Rizal St., Brgy. Poblacion, Baliuag')
        # No location complaint anywhere on the way out.
        self.assertFalse(
            [m for m in self.messages(response) if 'location' in m.lower()
             and 'placed' not in m.lower()]
        )

    def test_a_denied_browser_with_a_saved_address_also_goes_through(self):
        self.save_address()

        self.check_out(addr_contact=CONTACT, location_status='denied')

        self.assertEqual(SalesTransaction.objects.count(), 1)
        self.assertFalse(SalesTransaction.objects.get().has_delivery_pin)

    def test_denied_with_nothing_typed_is_refused_for_the_address_not_the_location(self):
        response = self.check_out(addr_contact=CONTACT, location_status='denied')

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(INCOMPLETE_ADDRESS, self.messages(response))

    def test_malformed_coordinates_are_dropped_not_fatal(self):
        for latitude, longitude in (('abc', '120.9'), ('91.000000', '120.9'),
                                    ('14.95', ''), ('nan', 'nan')):
            SalesTransaction.objects.all().delete()
            self.check_out(
                **TYPED, location_status='granted',
                delivery_latitude=latitude, delivery_longitude=longitude,
            )
            sale = SalesTransaction.objects.get()
            self.assertFalse(sale.has_delivery_pin, (latitude, longitude))


class SharingLocationTests(DeliveryCheckoutBase):
    def test_a_shared_pin_is_kept_rounded_to_four_decimal_places(self):
        self.check_out(
            **TYPED, location_status='granted',
            delivery_latitude='14.951266', delivery_longitude='120.900549',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.delivery_latitude, Decimal('14.951300'))
        self.assertEqual(sale.delivery_longitude, Decimal('120.900500'))
        self.assertTrue(sale.has_delivery_pin)
        self.assertIn('openstreetmap.org', sale.delivery_map_url)

    def test_a_pin_alone_satisfies_a_delivery(self):
        """Pinned location OR complete typed address."""
        self.check_out(
            addr_contact=CONTACT, address_source='typed', location_status='granted',
            delivery_latitude='14.9510', delivery_longitude='120.9000',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.delivery_address, 'Pinned location (14.9510, 120.9000)')
        self.assertTrue(sale.has_delivery_pin)

    def test_the_pin_saved_on_the_chosen_address_travels_with_the_order(self):
        self.save_address(latitude=Decimal('14.951234'), longitude=Decimal('120.900567'))

        self.check_out(addr_contact=CONTACT)

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.delivery_latitude, Decimal('14.951200'))
        self.assertEqual(sale.delivery_longitude, Decimal('120.900600'))

    def test_a_pin_saved_on_the_account_is_not_used_for_a_different_typed_address(self):
        self.save_address(latitude=Decimal('14.951234'), longitude=Decimal('120.900567'))

        self.check_out(**TYPED)

        self.assertFalse(SalesTransaction.objects.get().has_delivery_pin)

    def test_the_database_refuses_half_a_pin(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SalesTransaction.objects.create(
                branch=self.branch, customer=self.shopper,
                delivery_latitude=Decimal('14.951200'),
            )


class DeliveryNeedsAPlaceTests(DeliveryCheckoutBase):
    """Server-side: a delivery needs a pin or a complete typed address."""

    def test_nothing_typed_and_no_pin_is_refused(self):
        response = self.check_out(address_source='typed', addr_contact=CONTACT)

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(INCOMPLETE_ADDRESS, self.messages(response))
        # The cart survives.
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 2})

    def test_each_missing_part_of_a_typed_address_is_refused(self):
        for missing in ('addr_street', 'addr_barangay', 'addr_city'):
            payload = dict(TYPED)
            payload.pop(missing)
            response = self.check_out(**payload)
            self.assertEqual(SalesTransaction.objects.count(), 0, missing)
            self.assertIn(INCOMPLETE_ADDRESS, self.messages(response), missing)

    def test_whitespace_only_parts_do_not_count(self):
        payload = dict(TYPED, addr_street='   ')

        self.check_out(**payload)

        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_a_landmark_is_optional_and_is_recorded_when_given(self):
        self.check_out(**TYPED, addr_landmark='beside the chapel')

        self.assertEqual(
            SalesTransaction.objects.get().delivery_address,
            '14 Rizal St., Brgy. Poblacion, Baliuag (Landmark: beside the chapel)',
        )

    def test_a_landmark_alone_is_not_an_address(self):
        self.check_out(
            address_source='typed', addr_contact=CONTACT, addr_landmark='the chapel',
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_the_typed_fields_come_back_after_a_refusal_but_no_coordinates(self):
        # follow=True lands on the cart, which is the page that redisplays the
        # draft, so the redisplayed form is the response itself.
        response = self.check_out(
            address_source='typed', addr_street='14 Rizal St.', addr_contact=CONTACT,
            location_status='denied',
            delivery_latitude='14.9510', delivery_longitude='120.9000',
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertContains(response, 'value="14 Rizal St."')
        self.assertNotContains(response, '14.9510')
        # And the draft is single-use.
        again = self.client.get(reverse('orders:cart'))
        self.assertNotContains(again, 'value="14 Rizal St."')

    def test_over_long_parts_are_refused_with_a_clear_message(self):
        response = self.check_out(**dict(TYPED, addr_street='x' * 256))

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn('Street cannot be longer than 255 characters.', self.messages(response))

    def test_a_delivery_still_needs_a_contact_number(self):
        payload = dict(TYPED)
        payload.pop('addr_contact')

        response = self.check_out(**payload)

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn(NEEDS_CONTACT, self.messages(response))

    def test_the_contact_number_on_file_is_used_when_the_field_is_blank(self):
        self.set_contact_on_file('09181234567')
        payload = dict(TYPED)
        payload.pop('addr_contact')

        self.check_out(**payload)

        self.assertEqual(SalesTransaction.objects.get().delivery_contact_number, '09181234567')

    def test_an_invalid_contact_number_is_refused(self):
        response = self.check_out(**dict(TYPED, addr_contact='12345'))

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn('Enter a valid PH mobile number', ' '.join(self.messages(response)))


class PickupNeedsNeitherTests(DeliveryCheckoutBase):
    def test_pickup_carries_no_address_pin_or_contact_whatever_is_posted(self):
        self.check_out(
            fulfillment_type='Pickup',
            **TYPED, location_status='granted',
            delivery_latitude='14.9510', delivery_longitude='120.9000',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.fulfillment_type, SalesTransaction.FulfillmentType.PICKUP)
        self.assertEqual(sale.delivery_address, '')
        self.assertEqual(sale.delivery_contact_number, '')
        self.assertIsNone(sale.delivery_latitude)
        self.assertIsNone(sale.delivery_longitude)

    def test_pickup_is_never_refused_for_a_missing_address_or_location(self):
        response = self.check_out(fulfillment_type='Pickup')

        self.assertEqual(SalesTransaction.objects.count(), 1)
        self.assertNotIn(INCOMPLETE_ADDRESS, self.messages(response))

    def test_pickup_does_not_save_a_typed_address_to_the_account(self):
        self.check_out(fulfillment_type='Pickup', **TYPED, addr_save='1')

        self.assertFalse(Address.objects.filter(user=self.shopper).exists())


class SavingTheTypedAddressTests(DeliveryCheckoutBase):
    def test_ticking_save_keeps_the_address_but_never_the_pin(self):
        self.check_out(
            **TYPED, addr_save='1', location_status='granted',
            delivery_latitude='14.951266', delivery_longitude='120.900549',
        )

        address = Address.objects.get(user=self.shopper)
        self.assertEqual(address.text, '14 Rizal St., Brgy. Poblacion, Baliuag')
        self.assertEqual(address.street_detail, '14 Rizal St.')
        self.assertTrue(address.is_default)
        self.assertIsNone(address.latitude)
        self.assertIsNone(address.longitude)
        self.assertTrue(SalesTransaction.objects.get().has_delivery_pin)

    def test_the_saved_row_gets_psgc_codes_when_the_words_match(self):
        self.check_out(**dict(TYPED, addr_barangay='Poblacion'), addr_save='1')

        address = Address.objects.get(user=self.shopper)
        self.assertEqual(address.city, '031403')
        self.assertEqual(address.province, '0314')
        self.assertEqual(address.region, '03')
        self.assertEqual(address.barangay, '031403014')

    def test_an_unrecognised_place_is_saved_without_codes(self):
        self.check_out(**dict(TYPED, addr_city='Nowhereville'), addr_save='1')

        address = Address.objects.get(user=self.shopper)
        self.assertEqual(address.city, '')
        self.assertEqual(address.barangay, '')

    def test_without_the_tick_nothing_is_saved(self):
        self.check_out(**TYPED)

        self.assertFalse(Address.objects.filter(user=self.shopper).exists())

    def test_a_new_address_does_not_take_over_an_existing_default(self):
        existing = self.save_address()

        self.check_out(**TYPED, addr_save='1')

        existing.refresh_from_db()
        self.assertTrue(existing.is_default)
        self.assertFalse(Address.objects.exclude(pk=existing.pk).get().is_default)


class CartPageDeliveryBlockTests(DeliveryCheckoutBase):
    def test_the_block_offers_both_location_choices_and_the_privacy_note(self):
        page = self.client.get(reverse('orders:cart'))

        self.assertContains(page, 'Use my current location')
        self.assertContains(page, "Don't share my location")
        self.assertContains(page, 'Pin on map')
        self.assertContains(page, 'Data Privacy Act of 2012')
        self.assertContains(page, 'optional')

    def test_no_saved_address_means_the_typed_form_is_the_way(self):
        page = self.client.get(reverse('orders:cart'))

        self.assertContains(page, 'name="addr_street"')
        self.assertContains(page, 'You have no saved address')
        self.assertNotContains(page, 'Use my saved address')

    def test_a_saved_address_is_the_default_choice(self):
        self.save_address()

        page = self.client.get(reverse('orders:cart'))

        self.assertContains(page, 'Use my saved address')
        self.assertContains(page, 'Use a different address for this order')
        self.assertContains(page, '12 Real St, Baliuag')

    def test_the_contact_number_is_prefilled_from_the_account(self):
        self.set_contact_on_file('09181234567')

        page = self.client.get(reverse('orders:cart'))

        self.assertContains(page, 'value="09181234567"')


class DeliveryApiTests(DeliveryCheckoutBase):
    """The customer sales API applies the same rule."""

    def post(self, **extra):
        payload = {
            'transactionType': SalesTransaction.TransactionType.ONLINE,
            'modeOfPayment': 'Cash on Delivery',
            'fulfillmentType': SalesTransaction.FulfillmentType.DELIVERY,
            'items': [{'productId': self.product.pk, 'quantity': 1}],
        }
        payload.update(extra)
        return self.client.post('/api/sales/', payload, content_type='application/json')

    def test_a_delivery_with_neither_address_nor_pin_is_refused(self):
        response = self.post()

        self.assertEqual(response.status_code, 400)
        self.assertIn('deliveryAddress', response.json())
        self.assertEqual(SalesTransaction.objects.count(), 0)

    def test_a_pin_alone_is_accepted_and_rounded(self):
        response = self.post(deliveryLatitude='14.951266', deliveryLongitude='120.900549')

        self.assertEqual(response.status_code, 201, response.content)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.delivery_latitude, Decimal('14.951300'))
        self.assertEqual(sale.delivery_longitude, Decimal('120.900500'))

    def test_an_address_alone_is_accepted_with_no_coordinates(self):
        response = self.post(deliveryAddress='12 Real St, Baliuag')

        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(SalesTransaction.objects.get().has_delivery_pin)

    def test_a_malformed_pin_is_reported(self):
        response = self.post(
            deliveryAddress='12 Real St', deliveryLatitude='95.0', deliveryLongitude='120.9',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('deliveryLatitude', response.json())

    def test_half_a_pin_is_reported(self):
        response = self.post(deliveryAddress='12 Real St', deliveryLatitude='14.95')

        self.assertEqual(response.status_code, 400)

    def test_pickup_drops_any_pin_and_address(self):
        response = self.post(
            fulfillmentType=SalesTransaction.FulfillmentType.PICKUP,
            deliveryAddress='12 Real St', deliveryLatitude='14.95',
            deliveryLongitude='120.9', deliveryContactNumber=CONTACT,
        )

        self.assertEqual(response.status_code, 201, response.content)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.delivery_address, '')
        self.assertEqual(sale.delivery_contact_number, '')
        self.assertFalse(sale.has_delivery_pin)
