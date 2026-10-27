"""A customer can take a saved pin back, and only the typed address is used.

"Remove saved location" clears an address's latitude and longitude and leaves
everything typed. Afterwards the branch finder and delivery go by the typed
address (and its barangay/city codes), never by coordinates.
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import Address, User
from orders.services import customer_fulfillment_location


class RemoveSavedLocationTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='al-customer', password='pw', role=User.Role.CUSTOMER
        )
        self.address = Address.objects.create(
            user=self.customer, is_default=True,
            text='14 Rizal St., Brgy. Poblacion, Baliuag',
            street_detail='14 Rizal St.',
            region='03', province='0314', city='031403', barangay='031403014',
            latitude=Decimal('14.951234'), longitude=Decimal('120.900567'),
        )
        self.client.force_login(self.customer)

    def remove(self, address=None):
        address = address or self.address
        return self.client.post(
            reverse('accounts:address_remove_location', args=[address.pk]), follow=True
        )

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_removing_clears_the_coordinates_and_nothing_else(self):
        self.remove()

        self.address.refresh_from_db()
        self.assertIsNone(self.address.latitude)
        self.assertIsNone(self.address.longitude)
        self.assertEqual(self.address.text, '14 Rizal St., Brgy. Poblacion, Baliuag')
        self.assertEqual(self.address.street_detail, '14 Rizal St.')
        self.assertEqual(self.address.city, '031403')
        self.assertEqual(self.address.barangay, '031403014')
        self.assertTrue(self.address.is_default)

    def test_it_confirms_that_only_the_typed_address_is_used_afterwards(self):
        response = self.remove()

        self.assertIn(
            'Saved location removed. Only your typed address will be used for delivery.',
            self.messages(response),
        )

    def test_afterwards_the_typed_address_is_all_the_location_logic_sees(self):
        self.remove()

        location = customer_fulfillment_location(self.customer, self.client.session)

        self.assertIsNone(location['latitude'])
        self.assertIsNone(location['longitude'])
        self.assertEqual(location['city'], '031403')
        self.assertEqual(location['barangay'], '031403014')
        self.assertEqual(location['source'], 'address')
        self.assertEqual(location['label'], '14 Rizal St., Brgy. Poblacion, Baliuag')

    def test_it_also_drops_a_location_the_session_was_holding(self):
        session = self.client.session
        session['fulfillment_location'] = {
            'latitude': '14.95', 'longitude': '120.9', 'captured_at': 9999999999,
        }
        session['fulfillment_assignment'] = {'branch_id': 1}
        session.save()

        self.remove()

        self.assertNotIn('fulfillment_location', self.client.session)
        self.assertNotIn('fulfillment_assignment', self.client.session)

    def test_removing_twice_is_harmless_and_says_there_was_nothing_to_remove(self):
        self.remove()

        response = self.remove()

        self.assertIn(
            'That address had no saved location. Only the typed address is used.',
            self.messages(response),
        )

    def test_it_only_accepts_post(self):
        response = self.client.get(
            reverse('accounts:address_remove_location', args=[self.address.pk])
        )

        self.assertEqual(response.status_code, 405)
        self.address.refresh_from_db()
        self.assertIsNotNone(self.address.latitude)

    def test_nobody_can_remove_someone_elses_pin(self):
        other = User.objects.create_user(
            username='al-other', password='pw', role=User.Role.CUSTOMER
        )
        theirs = Address.objects.create(
            user=other, is_default=True, text='Elsewhere',
            latitude=Decimal('14.5'), longitude=Decimal('121.0'),
        )

        response = self.client.post(
            reverse('accounts:address_remove_location', args=[theirs.pk])
        )

        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNotNone(theirs.latitude)

    def test_it_needs_a_login(self):
        self.client.logout()

        response = self.client.post(
            reverse('accounts:address_remove_location', args=[self.address.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])
        self.address.refresh_from_db()
        self.assertIsNotNone(self.address.latitude)


class ProfileAddressCardTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='ap-customer', password='pw', role=User.Role.CUSTOMER
        )
        self.client.force_login(self.customer)

    def page(self):
        return self.client.get(reverse('accounts:profile'))

    def test_a_pinned_address_offers_removal(self):
        Address.objects.create(
            user=self.customer, is_default=True, text='Home',
            latitude=Decimal('14.951234'), longitude=Decimal('120.900567'),
        )

        page = self.page()

        self.assertContains(page, 'Pinned location saved.')
        self.assertContains(page, 'Remove saved location')
        self.assertContains(page, 'data-confirm-title="Remove saved location?"')

    def test_an_unpinned_address_says_so_and_offers_no_removal(self):
        Address.objects.create(user=self.customer, is_default=True, text='Home')

        page = self.page()

        self.assertContains(page, 'No pinned location saved. Only the typed address is used.')
        self.assertNotContains(page, 'address_remove_location')
        self.assertNotContains(page, 'data-confirm-title="Remove saved location?"')

    def test_the_privacy_note_is_near_the_addresses_and_the_map(self):
        Address.objects.create(user=self.customer, is_default=True, text='Home')

        page = self.page()

        self.assertContains(page, 'id="address-privacy-note"')
        self.assertContains(page, 'id="ap-privacy-note"')
        self.assertContains(page, 'Data Privacy Act of 2012')

    def test_an_address_can_be_saved_without_any_pin(self):
        response = self.client.post(reverse('accounts:address_create'), {
            'type': 'HOME', 'region': '03', 'province': '0314', 'city': '031403',
            'barangay': '031403014', 'street_detail': '14 Rizal St.',
            'text': '14 Rizal St., Poblacion, Baliuag', 'latitude': '', 'longitude': '',
        })

        self.assertEqual(response.status_code, 302)
        address = Address.objects.get(user=self.customer)
        self.assertIsNone(address.latitude)
        self.assertIsNone(address.longitude)
