"""Serviceable areas on the storefront.

What these pin down:

  * checkout() refuses a delivery to a place the branch does not serve, on the
    server, whatever the page showed - a hand-built POST gets the same answer;
  * the check uses the province / municipality / barangay chosen (or the saved
    address's), including "Other" words and a declined location;
  * a refused delivery can become a pickup, and that is confirmed in the app;
  * the "We deliver to:" banner is on the home page, the catalog and the
    cart/checkout page, can be dismissed for the session everywhere but the
    checkout, and says nothing when no area is configured;
  * the customer sales API applies the same rule, and a new customer is told
    where the store delivers exactly once.
"""
import time
from decimal import Decimal

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from accounts import geo
from accounts.models import Address, Notification, User
from branches import coverage as cov
from branches.models import Branch, ServiceArea
from branches.tests_service_areas import (
    BALIUAG, BULACAN, MALOLOS, PAMPANGA, POBLACION, TARCAN, Type, area,
)
from sales.models import SalesTransaction

from .delivery import INCOMPLETE_ADDRESS
from .service_areas import AREA_SWITCH_KEY
from .tests_delivery_location import CONTACT, DeliveryCheckoutBase

BULACAN_PLACE = {
    'addr_province': BULACAN.province_code,
    'addr_city': BALIUAG.city_code,
    'addr_barangay': POBLACION.barangay_code,
}
PAMPANGA_PLACE = {
    'addr_province': PAMPANGA.province_code,
    'addr_city': PAMPANGA.city_code,
    'addr_barangay': PAMPANGA.barangay_code,
}


class AreaCheckoutBase(DeliveryCheckoutBase):
    """A branch that delivers within Bulacan, and a shopper with a full cart."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.province_area = area(self.branch, BULACAN, Type.PROVINCE)

    def deliver(self, place=None, **extra):
        payload = {
            'address_source': 'typed', 'addr_street': '14 Rizal St.',
            'addr_contact': CONTACT,
        }
        payload.update(BULACAN_PLACE if place is None else place)
        payload.update(extra)
        return self.check_out(**payload)

    def pickup(self, **extra):
        return self.check_out(fulfillment_type='Pickup', **extra)

    def notices(self):
        return list(Notification.objects.filter(user=self.shopper, title='Your order was switched to pickup'))


class ServerSideEnforcementTests(AreaCheckoutBase):
    def test_a_delivery_inside_the_area_is_placed_and_records_the_place(self):
        self.deliver()

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.fulfillment_type, 'Delivery')
        self.assertEqual(sale.delivery_province_code, BULACAN.province_code)
        self.assertEqual(sale.delivery_city_code, BALIUAG.city_code)
        self.assertEqual(sale.delivery_barangay_code, POBLACION.barangay_code)
        self.assertEqual(sale.delivery_address, '14 Rizal St., Brgy. Poblacion, Baliuag, Bulacan')
        self.assertEqual(sale.delivery_area_label, 'Brgy. Poblacion, Baliuag, Bulacan')

    def test_a_delivery_outside_bulacan_is_refused_and_nothing_is_written(self):
        response = self.deliver(PAMPANGA_PLACE)

        self.assertFalse(SalesTransaction.objects.exists())
        self.assertRedirects(response, reverse('orders:cart'))
        text = ' '.join(self.messages(response))
        self.assertIn('outside Tarcan’s delivery areas', text)
        self.assertIn('Switch to Pickup or choose another branch', text)
        # The cart survives, and what they typed is kept: the cart page the
        # redirect lands on has already consumed the draft to refill the form.
        self.assertEqual(self.client.session['cart'], {str(self.product.pk): 2})
        self.assertIn(f'data-draft-province="{PAMPANGA.province_code}"', response.content.decode())

    def test_the_refusal_is_the_servers_not_the_pages(self):
        """No JavaScript ran, no check was asked for: a bare POST is still refused."""
        self.fill_cart()
        response = self.client.post(reverse('orders:checkout'), {
            'checkout_token': self.token, 'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Delivery', 'address_source': 'typed',
            'addr_street': '1 Anywhere', 'addr_contact': CONTACT, **PAMPANGA_PLACE,
        })

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SalesTransaction.objects.exists())

    def test_words_work_like_codes_including_the_common_spelling_baliwag(self):
        self.deliver({'addr_province': 'Bulacan', 'addr_city': 'Baliwag', 'addr_barangay': 'Poblacion'})

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.delivery_city_code, BALIUAG.city_code)
        self.assertEqual(sale.delivery_address, '14 Rizal St., Brgy. Poblacion, Baliwag, Bulacan')

    def test_other_words_are_checked_by_name(self):
        other = '__other__'
        self.deliver({
            'addr_province': other, 'addr_province_other': 'Bulacan',
            'addr_city': other, 'addr_city_other': 'Baliuag',
            'addr_barangay': other, 'addr_barangay_other': 'Poblacion',
        })
        self.assertEqual(SalesTransaction.objects.get().delivery_city_code, BALIUAG.city_code)

        SalesTransaction.objects.all().delete()
        self.deliver({
            'addr_province': other, 'addr_province_other': 'Pampanga',
            'addr_city': other, 'addr_city_other': 'Angeles',
            'addr_barangay': other, 'addr_barangay_other': 'Balibago',
        })
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_place_that_matches_nothing_is_refused_not_guessed(self):
        response = self.deliver({
            'addr_province': 'Bulacan', 'addr_city': 'Nowhereville', 'addr_barangay': 'Brgy. X',
        })

        self.assertFalse(SalesTransaction.objects.exists())
        self.assertIn('Nowhereville', ' '.join(self.messages(response)))

    def test_a_missing_barangay_is_refused_by_the_complete_address_rule(self):
        response = self.deliver({'addr_province': BULACAN.province_code, 'addr_city': BALIUAG.city_code,
                                 'addr_barangay': ''})

        self.assertFalse(SalesTransaction.objects.exists())
        self.assertIn(INCOMPLETE_ADDRESS, self.messages(response))

    def test_a_barangay_the_owner_excluded_is_refused(self):
        area(self.branch, TARCAN, Type.BARANGAY, active=False)

        response = self.deliver({**BULACAN_PLACE, 'addr_barangay': TARCAN.barangay_code})

        self.assertFalse(SalesTransaction.objects.exists())
        self.assertIn('does not deliver to', ' '.join(self.messages(response)))
        self.deliver()  # a barangay next door is still fine
        self.assertEqual(SalesTransaction.objects.count(), 1)

    def test_an_inactive_province_stops_all_deliveries_but_not_pickup(self):
        self.province_area.is_active = False
        self.province_area.save()

        self.deliver()
        self.assertFalse(SalesTransaction.objects.exists())
        self.pickup()
        self.assertEqual(SalesTransaction.objects.get().fulfillment_type, 'Pickup')

    def test_a_pin_alone_names_no_barangay_so_it_cannot_satisfy_a_branchs_areas(self):
        response = self.check_out(
            addr_contact=CONTACT, address_source='typed',
            location_status='granted', delivery_latitude='14.9500', delivery_longitude='120.9000',
        )

        self.assertFalse(SalesTransaction.objects.exists())
        self.assertTrue(any('province, municipality and barangay' in m for m in self.messages(response)))

    def test_a_pin_alone_still_works_at_a_branch_with_no_areas(self):
        ServiceArea.objects.all().delete()

        self.check_out(
            addr_contact=CONTACT, address_source='typed',
            location_status='granted', delivery_latitude='14.9500', delivery_longitude='120.9000',
        )

        self.assertEqual(SalesTransaction.objects.get().fulfillment_type, 'Delivery')

    def test_a_declined_location_is_checked_on_the_typed_place(self):
        outside = self.deliver(PAMPANGA_PLACE, location_status='declined')
        self.assertFalse(SalesTransaction.objects.exists())
        self.assertIn('outside', ' '.join(self.messages(outside)))

        self.deliver(location_status='declined', delivery_latitude='14.95', delivery_longitude='120.9')
        sale = SalesTransaction.objects.get()
        self.assertIsNone(sale.delivery_latitude)
        self.assertEqual(sale.delivery_city_code, BALIUAG.city_code)

    def test_a_shared_pin_does_not_override_an_outside_typed_place(self):
        self.deliver(PAMPANGA_PLACE, location_status='granted',
                     delivery_latitude='14.9500', delivery_longitude='120.9000')
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_saved_address_with_codes_is_checked(self):
        self.save_address(
            text='Saved, Angeles', province=PAMPANGA.province_code,
            city=PAMPANGA.city_code, barangay=PAMPANGA.barangay_code)
        outside = self.check_out(addr_contact=CONTACT)
        self.assertFalse(SalesTransaction.objects.exists())
        self.assertIn('outside', ' '.join(self.messages(outside)))

        Address.objects.all().delete()
        self.save_address(
            text='12 Real St, Poblacion, Baliuag', province=BULACAN.province_code,
            city=BALIUAG.city_code, barangay=POBLACION.barangay_code)
        self.check_out(addr_contact=CONTACT)
        self.assertEqual(SalesTransaction.objects.get().delivery_barangay_code, POBLACION.barangay_code)

    def test_a_saved_address_with_no_codes_cannot_be_checked_and_is_refused(self):
        self.save_address(text='12 Real St, Baliuag')

        response = self.check_out(addr_contact=CONTACT)

        self.assertFalse(SalesTransaction.objects.exists())
        said = ' '.join(self.messages(response))
        # Wording for a SAVED address: its lists are hidden until the shopper
        # picks "Use a different address", so the generic "choose from the
        # lists" would send them looking for something they cannot see.
        self.assertIn('Your saved address does not say which province', said)
        self.assertIn('Use a different address for this order', said)
        self.assertIn('Switch to Pickup', said)

    def test_the_same_shopper_can_still_type_an_address_for_this_order(self):
        self.save_address(text='12 Real St, Baliuag')

        self.deliver()

        self.assertEqual(SalesTransaction.objects.get().delivery_barangay_code, POBLACION.barangay_code)

    def test_a_branch_with_no_areas_is_not_restricted(self):
        ServiceArea.objects.all().delete()

        self.deliver(PAMPANGA_PLACE)

        self.assertEqual(SalesTransaction.objects.get().fulfillment_type, 'Delivery')

    def test_pickup_is_never_checked_and_records_no_place(self):
        self.pickup(**PAMPANGA_PLACE)

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.fulfillment_type, 'Pickup')
        self.assertEqual(
            (sale.delivery_province_code, sale.delivery_city_code, sale.delivery_barangay_code),
            ('', '', ''))

    def test_the_minimum_order_is_enforced_for_delivery_but_not_pickup(self):
        self.province_area.minimum_order = Decimal('1000.00')
        self.province_area.save()  # the cart is 2 x 112 = 224

        response = self.deliver()
        self.assertFalse(SalesTransaction.objects.exists())
        self.assertIn('at least ₱1,000.00', ' '.join(self.messages(response)))
        # Being under the minimum is not "outside the area": no switch is recorded.
        self.assertNotIn(AREA_SWITCH_KEY, self.client.session)

        self.pickup()
        self.assertEqual(SalesTransaction.objects.get().fulfillment_type, 'Pickup')

    def test_a_municipality_fee_and_minimum_apply_over_the_provinces(self):
        self.province_area.minimum_order = Decimal('1000.00')
        self.province_area.save()
        area(self.branch, BALIUAG, Type.MUNICIPALITY, minimum=Decimal('100.00'))

        self.deliver()  # Baliuag asks 100, not Bulacan's 1,000

        self.assertEqual(SalesTransaction.objects.get().fulfillment_type, 'Delivery')


class PickupSwitchTests(AreaCheckoutBase):
    def test_a_refused_delivery_then_a_pickup_order_is_confirmed_in_the_app(self):
        self.deliver(PAMPANGA_PLACE)
        self.assertIn(AREA_SWITCH_KEY, self.client.session)

        response = self.pickup()

        sale = SalesTransaction.objects.get()
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn(sale.order_number, notices[0].message)
        self.assertIn('Tarcan', notices[0].message)
        self.assertIn('Brgy. Balibago', notices[0].message)
        self.assertEqual(notices[0].action_url, reverse('orders:track_order', args=[sale.pk]))
        self.assertTrue(any('this order is for pickup at Tarcan' in m for m in self.messages(response)))
        self.assertNotIn(AREA_SWITCH_KEY, self.client.session)

    def test_a_pickup_with_no_earlier_refusal_gets_no_switch_notice(self):
        self.pickup()
        self.assertEqual(self.notices(), [])

    def test_the_notice_is_sent_once_not_for_every_later_order(self):
        self.deliver(PAMPANGA_PLACE)
        self.pickup()
        self.pickup()

        self.assertEqual(SalesTransaction.objects.count(), 2)
        self.assertEqual(len(self.notices()), 1)

    def test_a_delivery_placed_after_a_refusal_clears_it_and_sends_no_notice(self):
        self.deliver(PAMPANGA_PLACE)
        self.deliver()

        self.assertEqual(SalesTransaction.objects.get().fulfillment_type, 'Delivery')
        self.assertNotIn(AREA_SWITCH_KEY, self.client.session)
        self.assertEqual(self.notices(), [])

    def test_a_refusal_at_another_branch_or_long_ago_is_not_claimed(self):
        for switch in (
            {'branch_id': self.branch.pk + 1000, 'place': 'Somewhere', 'at': int(time.time())},
            {'branch_id': self.branch.pk, 'place': 'Somewhere', 'at': int(time.time()) - 24 * 3600},
        ):
            with self.subTest(switch=switch):
                session = self.client.session
                session[AREA_SWITCH_KEY] = switch
                session.save()
                self.pickup()
                self.assertEqual(self.notices(), [])

    def test_the_switch_button_records_the_switch_only_for_a_place_that_is_really_outside(self):
        url = reverse('orders:area_switch_to_pickup')

        inside = self.client.post(url, {'address_source': 'typed', **BULACAN_PLACE})
        self.assertEqual(inside.json()['recorded'], False)
        self.assertNotIn(AREA_SWITCH_KEY, self.client.session)

        outside = self.client.post(url, {'address_source': 'typed', **PAMPANGA_PLACE})
        self.assertEqual(outside.json()['recorded'], True)
        self.pickup()
        self.assertEqual(len(self.notices()), 1)

    def test_the_switch_endpoint_needs_a_customer_and_a_post(self):
        url = reverse('orders:area_switch_to_pickup')
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(url, PAMPANGA_PLACE).status_code, 302)


class LiveCheckEndpointTests(AreaCheckoutBase):
    def check(self, place=None, **extra):
        params = {'address_source': 'typed', **(BULACAN_PLACE if place is None else place), **extra}
        return self.client.get(reverse('orders:area_check'), params)

    def test_an_inside_address_reports_ok_with_fee_and_minimum(self):
        self.province_area.delivery_fee = Decimal('100.00')
        self.province_area.minimum_order = Decimal('50.00')
        self.province_area.save()

        data = self.check().json()

        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'covered')
        self.assertEqual(data['delivery_fee'], '100.00')
        self.assertEqual(data['minimum_order'], '50.00')
        self.assertEqual(data['place'], 'Brgy. Poblacion, Baliuag, Bulacan')

    def test_an_outside_address_says_so_and_names_branches_that_do_deliver_there(self):
        other = Branch.objects.create(branch_name='Makinabang', location='Baliuag', owner=self.owner)
        area(other, PAMPANGA, Type.PROVINCE)

        data = self.check(PAMPANGA_PLACE).json()

        self.assertFalse(data['ok'])
        self.assertEqual(data['status'], 'outside')
        self.assertIn('outside Tarcan', data['message'])
        self.assertEqual(data['other_branches'], ['Makinabang'])

    def test_an_unfinished_address_is_incomplete_not_outside(self):
        data = self.check({'addr_province': BULACAN.province_code}).json()
        self.assertEqual(data['status'], 'incomplete')
        self.assertFalse(data['ok'])

    def test_a_branch_with_no_areas_reports_unconfigured_and_ok(self):
        ServiceArea.objects.all().delete()
        data = self.check(PAMPANGA_PLACE).json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['configured'])

    def test_it_checks_the_saved_address_unless_a_different_one_is_chosen(self):
        self.save_address(province=PAMPANGA.province_code, city=PAMPANGA.city_code,
                          barangay=PAMPANGA.barangay_code)

        self.assertFalse(self.check(address_source='saved').json()['ok'])
        self.assertTrue(self.check(address_source='typed').json()['ok'])

    def test_a_saved_address_with_no_codes_gets_saved_address_wording_live_too(self):
        self.save_address(text='12 Real St, Baliuag')

        saved = self.check(address_source='saved').json()
        typed = self.check({'addr_province': BULACAN.province_code}, address_source='typed').json()

        self.assertEqual(saved['status'], 'incomplete')
        self.assertIn('Your saved address does not say', saved['message'])
        self.assertIn('Choose your province, municipality and barangay', typed['message'])

    def test_the_minimum_uses_the_carts_own_total(self):
        self.province_area.minimum_order = Decimal('1000.00')
        self.province_area.save()

        data = self.check().json()

        self.assertEqual(data['status'], 'below_minimum')
        self.assertFalse(data['ok'])

    def test_only_a_signed_in_customer_may_ask(self):
        self.client.logout()
        self.assertEqual(self.check().status_code, 302)
        staff = User.objects.create_user(username='ac-staff', password='pw', role=User.Role.STAFF)
        self.client.force_login(staff)
        self.assertEqual(self.check().status_code, 403)

    def test_without_a_branch_there_is_nothing_to_check(self):
        session = self.client.session
        session.pop('branch_id')
        session.save()
        self.assertEqual(self.check().status_code, 409)


class CartPageTests(AreaCheckoutBase):
    def test_the_address_is_a_province_municipality_barangay_chain_with_other(self):
        html = self.client.get(reverse('orders:cart')).content.decode()

        for name in ('addr_province', 'addr_city', 'addr_barangay',
                     'addr_province_other', 'addr_city_other', 'addr_barangay_other'):
            self.assertIn(f'name="{name}"', html)
        self.assertIn(f'<option value="{BULACAN.province_code}"', html)
        self.assertIn('Other…', html)
        self.assertNotIn('<input id="addr_city" type="text"', html)
        # the inline answer and its two ways out
        self.assertIn('id="area-check"', html)
        self.assertIn('Switch to Pickup', html)
        self.assertIn('Choose another branch', html)
        self.assertIn('checkout-service-area.js', html)

    def test_a_refused_submit_redisplays_what_was_typed(self):
        # The refusal redirects to the cart, which is the page that redisplays.
        html = self.deliver(PAMPANGA_PLACE).content.decode()

        self.assertIn(f'data-draft-province="{PAMPANGA.province_code}"', html)
        self.assertIn(f'data-draft-city="{PAMPANGA.city_code}"', html)
        self.assertIn('value="14 Rizal St."', html)


class BannerTests(AreaCheckoutBase):
    TEXT = 'We deliver to:'

    def page(self, name, *args):
        return self.client.get(reverse(name, args=args)).content.decode()

    def test_it_is_on_the_home_page_the_catalog_and_the_cart(self):
        for name in ('accounts:home', 'orders:catalog', 'orders:cart'):
            with self.subTest(page=name):
                html = self.page(name)
                self.assertIn(self.TEXT, html)
                self.assertIn('all of Bulacan', html)
                self.assertIn('Choose Pickup', html)
                self.assertIn('View all serviceable areas', html)
                self.assertIn(reverse('orders:service_areas'), html)

    def test_it_is_part_of_the_sticky_header(self):
        html = self.page('orders:catalog')
        header = html[html.index('<header class="shop-header">'):html.index('</header>')]
        self.assertIn('data-service-banner', header)

    def test_it_is_not_on_other_pages(self):
        self.assertNotIn(self.TEXT, self.page('orders:product_detail', self.product.pk))
        self.client.logout()
        self.assertNotIn(self.TEXT, self.page('accounts:login'))
        self.assertNotIn(self.TEXT, self.page('about'))

    def test_guests_see_it_too(self):
        self.client.logout()
        self.assertIn(self.TEXT, self.page('accounts:home'))
        self.assertIn(self.TEXT, self.page('orders:catalog'))

    def test_nothing_is_drawn_when_no_branch_has_areas(self):
        ServiceArea.objects.all().delete()
        for name in ('accounts:home', 'orders:catalog', 'orders:cart'):
            with self.subTest(page=name):
                self.assertNotIn('data-service-banner', self.page(name))

    def test_it_can_be_dismissed_for_the_session_but_returns_on_checkout(self):
        self.assertIn('data-service-banner-dismiss', self.page('orders:catalog'))

        response = self.client.post(reverse('orders:service_banner_dismiss'),
                                    {'next': reverse('orders:catalog')})
        self.assertRedirects(response, reverse('orders:catalog'))

        self.assertNotIn('data-service-banner', self.page('orders:catalog'))
        self.assertNotIn('data-service-banner', self.page('accounts:home'))
        cart = self.page('orders:cart')
        self.assertIn(self.TEXT, cart)
        self.assertNotIn('data-service-banner-dismiss', cart)

    def test_dismissing_belongs_to_one_session(self):
        self.client.post(reverse('orders:service_banner_dismiss'))
        self.client.logout()
        self.client.cookies.clear()
        self.assertIn(self.TEXT, self.page('accounts:home'))

    def test_dismiss_by_script_answers_json_and_refuses_an_off_site_next(self):
        ajax = self.client.post(reverse('orders:service_banner_dismiss'), {},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(ajax.json(), {'ok': True})

        response = self.client.post(reverse('orders:service_banner_dismiss'),
                                    {'next': 'https://evil.example/'})
        self.assertRedirects(response, reverse('orders:catalog'))

    def test_dismiss_needs_a_post(self):
        self.assertEqual(self.client.get(reverse('orders:service_banner_dismiss')).status_code, 405)

    def test_it_describes_the_shoppers_own_branch(self):
        other = Branch.objects.create(branch_name='Makinabang', location='Baliuag', owner=self.owner)
        area(other, MALOLOS, Type.MUNICIPALITY)

        self.assertIn('all of Bulacan', self.page('orders:catalog'))

        session = self.client.session
        session['branch_id'] = other.pk
        session.save()
        html = self.page('orders:catalog')
        self.assertIn('We deliver to:</strong> City Of Malolos (Capital)', html)
        self.assertNotIn('all of Bulacan', html)

    def test_when_nothing_is_delivered_it_says_to_choose_pickup(self):
        self.province_area.is_active = False
        self.province_area.save()

        html = self.page('orders:catalog')

        self.assertIn('Delivery isn’t available right now', html)
        self.assertNotIn(self.TEXT, html)


class PublicAreasPageTests(AreaCheckoutBase):
    def test_guests_can_read_every_branchs_areas_with_fees_and_minimums(self):
        self.province_area.delivery_fee = Decimal('100.00')
        self.province_area.minimum_order = Decimal('300.00')
        self.province_area.save()
        area(self.branch, BALIUAG, Type.MUNICIPALITY, fee=Decimal('0'))
        area(self.branch, TARCAN, Type.BARANGAY, active=False)
        other = Branch.objects.create(branch_name='Makinabang', location='Baliuag', owner=self.owner)
        area(other, MALOLOS, Type.MUNICIPALITY)
        self.client.logout()

        response = self.client.get(reverse('orders:service_areas'))
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertLess(html.index('Makinabang'), html.index('Tarcan', html.index('Makinabang')))
        self.assertIn('₱100.00', html)
        self.assertIn('₱300.00', html)
        self.assertIn('Free', html)
        self.assertIn('Not delivered to:', html)
        self.assertIn('City Of Malolos (Capital)', html)

    def test_it_says_so_when_nothing_has_been_published(self):
        ServiceArea.objects.all().delete()
        html = self.client.get(reverse('orders:service_areas')).content.decode()
        self.assertIn('haven’t been published yet', html)

    def test_a_back_link_only_goes_to_this_site(self):
        ok = self.client.get(reverse('orders:service_areas'), {'next': '/shop/cart/'})
        self.assertContains(ok, 'href="/shop/cart/"')
        bad = self.client.get(reverse('orders:service_areas'), {'next': 'https://evil.example/'})
        self.assertNotContains(bad, 'evil.example')


class SalesApiTests(AreaCheckoutBase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_login(self.shopper)

    def create(self, **override):
        body = {
            'transactionType': 'Online', 'modeOfPayment': 'Cash on Delivery',
            'fulfillmentType': 'Delivery', 'deliveryAddress': '14 Rizal St., Baliuag',
            'deliveryContactNumber': CONTACT,
            'items': [{'productId': self.product.pk, 'quantity': 2}],
        }
        body.update(override)
        return self.api.post('/api/sales/', body, format='json')

    def place(self, place):
        return {
            'deliveryProvince': place['addr_province'], 'deliveryCity': place['addr_city'],
            'deliveryBarangay': place['addr_barangay'],
        }

    def test_a_delivery_outside_the_areas_is_refused_and_writes_nothing(self):
        response = self.create(**self.place(PAMPANGA_PLACE))

        self.assertEqual(response.status_code, 400)
        self.assertIn('outside', str(response.json()['deliveryAddress']))
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_delivery_that_names_no_place_cannot_be_checked_and_is_refused(self):
        response = self.create()

        self.assertEqual(response.status_code, 400)
        self.assertIn('deliveryAddress', response.json())
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_delivery_inside_is_created_with_the_place_recorded(self):
        response = self.create(**self.place(BULACAN_PLACE))

        self.assertEqual(response.status_code, 201, response.content)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.delivery_city_code, BALIUAG.city_code)
        self.assertEqual(sale.delivery_barangay_code, POBLACION.barangay_code)

    def test_the_minimum_order_applies_to_the_api_too(self):
        self.province_area.minimum_order = Decimal('1000.00')
        self.province_area.save()

        response = self.create(**self.place(BULACAN_PLACE))

        self.assertEqual(response.status_code, 400)
        self.assertIn('at least', str(response.json()))

    def test_pickup_and_unconfigured_branches_are_untouched(self):
        self.assertEqual(self.create(fulfillmentType='Pickup', deliveryAddress='').status_code, 201)
        ServiceArea.objects.all().delete()
        self.assertEqual(self.create().status_code, 201)


class RegistrationNoticeTests(AreaCheckoutBase):
    def register(self, username='newshopper'):
        self.client.logout()
        return self.client.post(reverse('accounts:register'), {
            'username': username, 'email': f'{username}@example.com',
            'password1': 'Str0ngPass!2026', 'password2': 'Str0ngPass!2026',
            'first_name': 'New', 'last_name': 'Shopper', 'contact_number': '',
            'address': '12 Real St, Poblacion, Baliuag, Bulacan', 'accept_terms': '1',
        }, follow=True)

    def test_a_new_customer_is_told_where_we_deliver_once(self):
        response = self.register()

        user = User.objects.get(username='newshopper')
        notices = Notification.objects.filter(user=user, title='Where we deliver')
        self.assertEqual(notices.count(), 1)
        notice = notices.get()
        self.assertIn('all of Bulacan', notice.message)
        self.assertIn('Choose Pickup', notice.message)
        self.assertEqual(notice.action_url, reverse('orders:service_areas'))
        self.assertTrue(any('We deliver to: all of Bulacan' in str(m)
                            for m in get_messages(response.wsgi_request)))

    def test_no_notice_when_there_is_nothing_to_say(self):
        ServiceArea.objects.all().delete()

        self.register()

        self.assertFalse(Notification.objects.filter(title='Where we deliver').exists())

    def test_signing_in_again_does_not_repeat_it(self):
        self.register()
        self.client.logout()
        self.client.post(reverse('accounts:login'), {'username': 'newshopper', 'password': 'Str0ngPass!2026'})
        self.client.get(reverse('accounts:home'))

        self.assertEqual(Notification.objects.filter(title='Where we deliver').count(), 1)
