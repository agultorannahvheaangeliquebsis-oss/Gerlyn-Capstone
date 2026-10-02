from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Address, User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesItem, SalesTransaction

from .services import haversine_distance_km, resolve_nearest_branch


class NearestBranchServiceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='fulfillment-owner', password='pw', role=User.Role.OWNER
        )
        self.product_a = Product.objects.create(
            sku_code='FUL-A', product_name='Rice', unit_of_measure='Sack',
            unit_price=Decimal('100.00'),
        )
        self.product_b = Product.objects.create(
            sku_code='FUL-B', product_name='Oil', unit_of_measure='Bottle',
            unit_price=Decimal('80.00'),
        )

    def branch(self, name, latitude=None, longitude=None, active=True):
        return Branch.objects.create(
            branch_name=name,
            location='Baliuag',
            owner=self.owner,
            latitude=latitude,
            longitude=longitude,
            is_active=active,
        )

    def stock(self, branch, product, quantity):
        return Inventory.objects.create(
            branch=branch, product=product,
            quantity_on_hand=quantity, reorder_level=0,
        )

    def test_haversine_uses_straight_line_distance(self):
        distance = haversine_distance_km(0, 0, 0, 1)
        self.assertAlmostEqual(distance, 111.195, places=3)

    def test_correct_nearest_branch_is_chosen(self):
        near = self.branch('Near', Decimal('14.950000'), Decimal('120.900000'))
        far = self.branch('Far', Decimal('15.250000'), Decimal('120.900000'))
        for branch in (near, far):
            self.stock(branch, self.product_a, 5)

        result = resolve_nearest_branch(
            Decimal('14.951000'), Decimal('120.900000'), {self.product_a.pk: 2}
        )

        self.assertEqual(result.branch, near)
        self.assertIsNotNone(result.distance_km)

    def test_next_nearest_branch_is_used_for_the_complete_cart(self):
        near = self.branch('Near', Decimal('14.950000'), Decimal('120.900000'))
        farther = self.branch('Farther', Decimal('15.000000'), Decimal('120.900000'))
        self.stock(near, self.product_a, 5)
        self.stock(near, self.product_b, 0)
        self.stock(farther, self.product_a, 5)
        self.stock(farther, self.product_b, 5)

        result = resolve_nearest_branch(
            Decimal('14.951000'), Decimal('120.900000'),
            {self.product_a.pk: 1, self.product_b.pk: 1},
        )

        self.assertEqual(result.branch, farther)

    def test_order_is_not_split_when_no_branch_has_every_item(self):
        first = self.branch('First', Decimal('14.950000'), Decimal('120.900000'))
        second = self.branch('Second', Decimal('15.000000'), Decimal('120.900000'))
        self.stock(first, self.product_a, 5)
        self.stock(second, self.product_b, 5)

        result = resolve_nearest_branch(
            Decimal('14.951000'), Decimal('120.900000'),
            {self.product_a.pk: 1, self.product_b.pk: 1},
        )

        self.assertIsNone(result.branch)
        self.assertEqual(result.strategy, 'unavailable')

    @override_settings(DEFAULT_FULFILLMENT_BRANCH_ID=None)
    def test_missing_coordinates_fall_back_deterministically(self):
        first = self.branch('First without a pin')
        self.branch('Inactive nearby', Decimal('14.950000'), Decimal('120.900000'), active=False)
        self.stock(first, self.product_a, 2)

        result = resolve_nearest_branch(None, None, {self.product_a.pk: 1})

        self.assertEqual(result.branch, first)
        self.assertIsNone(result.distance_km)

    def test_configured_default_branch_is_used_without_location(self):
        first = self.branch('First')
        configured = self.branch('Configured default')
        self.stock(first, self.product_a, 2)
        self.stock(configured, self.product_a, 2)

        with override_settings(DEFAULT_FULFILLMENT_BRANCH_ID=configured.pk):
            result = resolve_nearest_branch(None, None, {self.product_a.pk: 1})

        self.assertEqual(result.branch, configured)

    def test_service_area_match_precedes_default_branch(self):
        configured = self.branch('Configured default')
        matched = self.branch('Matched service area')
        matched.city = '031403'
        matched.barangay = '031403001'
        matched.save(update_fields=['city', 'barangay'])
        self.stock(configured, self.product_a, 2)
        self.stock(matched, self.product_a, 2)

        with override_settings(DEFAULT_FULFILLMENT_BRANCH_ID=configured.pk):
            result = resolve_nearest_branch(
                None, None, {self.product_a.pk: 1},
                city='031403', barangay='031403001',
            )

        self.assertEqual(result.branch, matched)

    def test_branch_coordinate_validation_requires_pair_and_ranges(self):
        incomplete = Branch(
            branch_name='Incomplete', location='Baliuag', owner=self.owner,
            latitude=Decimal('14.900000'),
        )
        with self.assertRaises(ValidationError):
            incomplete.full_clean()

        out_of_range = Branch(
            branch_name='Invalid', location='Baliuag', owner=self.owner,
            latitude=Decimal('91'), longitude=Decimal('121'),
        )
        with self.assertRaises(ValidationError):
            out_of_range.full_clean()


class AutomaticFulfillmentWorkflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='workflow-owner', password='pw', role=User.Role.OWNER
        )
        self.customer = User.objects.create_user(
            username='workflow-customer', password='pw', role=User.Role.CUSTOMER
        )
        self.near = Branch.objects.create(
            branch_name='Near Branch', location='Near', owner=self.owner,
            latitude=Decimal('14.950000'), longitude=Decimal('120.900000'),
        )
        self.far = Branch.objects.create(
            branch_name='Far Branch', location='Far', owner=self.owner,
            latitude=Decimal('15.050000'), longitude=Decimal('120.900000'),
        )
        self.product_a = Product.objects.create(
            sku_code='WF-A', product_name='Rice', unit_of_measure='Sack',
            unit_price=Decimal('100.00'),
        )
        self.product_b = Product.objects.create(
            sku_code='WF-B', product_name='Oil', unit_of_measure='Bottle',
            unit_price=Decimal('80.00'),
        )
        self.client.force_login(self.customer)

    def stock(self, branch, product, quantity):
        return Inventory.objects.create(
            branch=branch, product=product,
            quantity_on_hand=quantity, reorder_level=0,
        )

    def share_location(self, latitude='14.951000', longitude='120.900000'):
        return self.client.post(reverse('orders:select_branch'), {
            'location_status': 'granted',
            'latitude': latitude,
            'longitude': longitude,
            'accuracy': '125',
            'next': reverse('orders:catalog'),
        })

    @override_settings(GEOCODING_REVERSE_URL='https://geocoder.invalid/reverse')
    @patch('accounts.geocoding.reverse_geocode', return_value='Poblacion, Baliuag, Bulacan')
    def test_detected_location_and_selected_branch_are_confirmed_separately(self, mocked):
        self.stock(self.near, self.product_a, 5)
        session = self.client.session
        session['cart'] = {str(self.product_a.pk): 1}
        session.save()

        response = self.share_location()

        self.assertRedirects(
            response,
            reverse('orders:select_branch') + '?next=%2Fshop%2F',
            fetch_redirect_response=False,
        )
        confirmation = self.client.get(response.url)
        self.assertContains(confirmation, 'Poblacion, Baliuag, Bulacan')
        self.assertContains(confirmation, 'Detected from this device')
        self.assertContains(confirmation, 'approximately 125 m')
        self.assertContains(confirmation, '<strong>Near Branch</strong>', html=True)
        self.assertContains(confirmation, 'straight-line distance')
        mocked.assert_called_once_with(Decimal('14.951000'), Decimal('120.900000'))

    def test_fallback_confirmation_identifies_saved_address_source(self):
        self.stock(self.near, self.product_a, 5)
        Address.objects.create(
            user=self.customer, is_default=True, text='12 Saved Street, Baliuag',
            latitude=Decimal('14.951000'), longitude=Decimal('120.900000'),
        )

        response = self.client.post(
            reverse('orders:select_branch'),
            {'location_status': 'fallback', 'next': reverse('orders:catalog')},
        )
        confirmation = self.client.get(response.url)

        self.assertContains(confirmation, '12 Saved Street, Baliuag')
        self.assertContains(confirmation, 'Using your default saved address')

    def test_whole_cart_moves_to_next_branch_without_being_cleared(self):
        self.stock(self.near, self.product_a, 5)
        self.stock(self.far, self.product_a, 5)
        self.stock(self.far, self.product_b, 5)
        self.share_location()

        self.client.post(reverse('orders:add_to_cart', args=[self.product_a.pk]))
        self.assertEqual(self.client.session['branch_id'], self.near.pk)
        self.client.post(reverse('orders:add_to_cart', args=[self.product_b.pk]))

        self.assertEqual(self.client.session['branch_id'], self.far.pk)
        self.assertEqual(
            self.client.session['cart'],
            {str(self.product_a.pk): 1, str(self.product_b.pk): 1},
        )
        response = self.client.get(reverse('orders:cart'))
        self.assertContains(response, 'Fulfilled by <strong>Far Branch</strong>')
        self.assertContains(response, 'approximately')
        self.assertContains(response, 'Location used:')
        self.assertContains(response, '14.9510, 120.9000')

    def test_add_refuses_to_split_the_cart(self):
        self.stock(self.near, self.product_a, 5)
        self.stock(self.far, self.product_b, 5)
        self.share_location()

        self.client.post(reverse('orders:add_to_cart', args=[self.product_a.pk]))
        response = self.client.post(
            reverse('orders:add_to_cart', args=[self.product_b.pk]), follow=True
        )

        self.assertEqual(self.client.session['cart'], {str(self.product_a.pk): 1})
        self.assertContains(response, 'currently be fulfilled by one branch')
        self.assertContains(response, 'Rice')
        self.assertContains(response, 'Oil')

    @override_settings(DEFAULT_FULFILLMENT_BRANCH_ID=None)
    def test_denied_geolocation_uses_fallback(self):
        self.stock(self.near, self.product_a, 5)
        session = self.client.session
        session['cart'] = {str(self.product_a.pk): 1}
        session.save()

        response = self.client.post(
            reverse('orders:select_branch'), {'location_status': 'denied'}, follow=True
        )

        self.assertEqual(self.client.session['branch_id'], self.near.pk)
        self.assertNotIn('fulfillment_location', self.client.session)
        self.assertContains(response, 'Location was not shared')

    def test_saved_address_change_recalculates_and_revalidates(self):
        self.stock(self.near, self.product_a, 5)
        self.stock(self.far, self.product_a, 5)
        address = Address.objects.create(
            user=self.customer, is_default=True, text='Near address',
            latitude=Decimal('14.951000'), longitude=Decimal('120.900000'),
        )
        session = self.client.session
        session['cart'] = {str(self.product_a.pk): 1}
        session['branch_id'] = self.near.pk
        session.save()

        self.client.get(reverse('orders:cart'))
        self.assertEqual(self.client.session['branch_id'], self.near.pk)
        address.latitude = Decimal('15.049000')
        address.longitude = Decimal('120.900000')
        address.save(update_fields=['latitude', 'longitude'])
        self.client.get(reverse('orders:cart'))

        self.assertEqual(self.client.session['branch_id'], self.far.pk)

    def test_checkout_ignores_tampered_branch_id(self):
        self.stock(self.near, self.product_a, 5)
        self.stock(self.far, self.product_a, 5)
        self.share_location(latitude='15.049000')
        self.client.post(reverse('orders:add_to_cart', args=[self.product_a.pk]))
        self.client.get(reverse('orders:cart'))
        token = self.client.session['checkout_token']

        self.client.post(reverse('orders:checkout'), {
            'checkout_token': token,
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Pickup',
            'branch_id': self.near.pk,
        })

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.branch, self.far)
        self.assertNotIn('fulfillment_location', self.client.session)

    def test_customer_sales_api_ignores_tampered_branch_id(self):
        self.stock(self.near, self.product_a, 5)
        self.stock(self.far, self.product_a, 5)
        self.share_location(latitude='15.049000')

        response = self.client.post(
            '/api/sales/',
            {
                'branchId': self.near.pk,
                'transactionType': SalesTransaction.TransactionType.ONLINE,
                'modeOfPayment': 'Cash on Delivery',
                'fulfillmentType': SalesTransaction.FulfillmentType.PICKUP,
                'items': [{'productId': self.product_a.pk, 'quantity': 1}],
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(SalesTransaction.objects.get().branch, self.far)
        self.assertNotIn('fulfillment_location', self.client.session)

    def test_open_order_commitments_prevent_overcommit(self):
        self.stock(self.near, self.product_a, 5)
        existing = SalesTransaction.objects.create(
            branch=self.near, customer=self.customer,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PENDING,
            fulfillment_type=SalesTransaction.FulfillmentType.PICKUP,
        )
        SalesItem.objects.create(
            transaction=existing, product=self.product_a,
            quantity_sold=4, unit_price=Decimal('100.00'),
        )
        session = self.client.session
        session['branch_id'] = self.near.pk
        session['cart'] = {str(self.product_a.pk): 2}
        session['checkout_token'] = 'commitment-token'
        session.save()

        response = self.client.post(reverse('orders:checkout'), {
            'checkout_token': 'commitment-token',
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Pickup',
            'branch_id': self.far.pk,
        }, follow=True)

        self.assertEqual(SalesTransaction.objects.count(), 1)
        self.assertContains(response, 'currently be fulfilled by one branch')
        self.assertContains(response, 'Rice')


class NoSingleBranchMessageTests(TestCase):
    """_no_single_branch_message names what is blocking the order, and falls
    back to the flat wording when there is nothing to name."""

    def test_it_names_the_products_that_fell_short(self):
        from orders.customer_views import _no_single_branch_message
        from orders.services import FulfillmentResolution

        rice = Product.objects.create(
            sku_code='NSB-A', product_name='Rice', unit_price=Decimal('100.00'),
        )
        oil = Product.objects.create(
            sku_code='NSB-B', product_name='Oil', unit_price=Decimal('80.00'),
        )
        message = _no_single_branch_message(
            FulfillmentResolution(
                branch=None, distance_km=None, strategy='none',
                unavailable_product_ids=(rice.pk, oil.pk),
            )
        )

        self.assertIn('Rice', message)
        self.assertIn('Oil', message)
        self.assertIn('currently be fulfilled by one branch', message)

    def test_no_ids_falls_back_to_the_flat_message(self):
        from orders.customer_views import NO_SINGLE_BRANCH, _no_single_branch_message
        from orders.services import FulfillmentResolution

        self.assertEqual(
            _no_single_branch_message(
                FulfillmentResolution(branch=None, distance_km=None, strategy='none')
            ),
            NO_SINGLE_BRANCH,
        )

    def test_ids_that_no_longer_name_a_product_fall_back_too(self):
        from orders.customer_views import NO_SINGLE_BRANCH, _no_single_branch_message
        from orders.services import FulfillmentResolution

        self.assertEqual(
            _no_single_branch_message(
                FulfillmentResolution(
                    branch=None, distance_km=None, strategy='none',
                    unavailable_product_ids=(999999,),
                )
            ),
            NO_SINGLE_BRANCH,
        )
