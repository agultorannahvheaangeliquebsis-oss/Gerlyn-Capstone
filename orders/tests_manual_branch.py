"""A shopper can choose their own branch, and the server keeps it.

Automatic assignment (orders.services.resolve_nearest_branch) stays the
default. "I'll choose a branch" adds an override for the session:

  * the choice is re-validated on the server (active branch, whole cart
    covered) and never taken from checkout or the sales API;
  * a chosen branch that cannot cover an item is NEVER silently swapped for
    another one — the add is refused and the cart stays where it is;
  * a branch that cannot cover the cart when it is chosen asks first
    (reduce / remove / stay) instead of changing anything;
  * if the chosen branch closes, the choice is dropped and the automatic rule
    takes over, with a message.
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import Address, User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction

from .services import branch_options, resolve_manual_branch


class ManualBranchBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='mb-owner', password='pw', role=User.Role.OWNER
        )
        self.customer = User.objects.create_user(
            username='mb-customer', password='pw', role=User.Role.CUSTOMER
        )
        self.near = Branch.objects.create(
            branch_name='Near Branch', location='Near St', owner=self.owner,
            latitude=Decimal('14.950000'), longitude=Decimal('120.900000'),
        )
        self.far = Branch.objects.create(
            branch_name='Far Branch', location='Far St', owner=self.owner,
            latitude=Decimal('15.050000'), longitude=Decimal('120.900000'),
        )
        self.rice = Product.objects.create(
            sku_code='MB-A', product_name='Rice', unit_of_measure='Sack',
            unit_price=Decimal('100.00'),
        )
        self.oil = Product.objects.create(
            sku_code='MB-B', product_name='Oil', unit_of_measure='Bottle',
            unit_price=Decimal('80.00'),
        )
        self.client.force_login(self.customer)

    def stock(self, branch, product, quantity):
        return Inventory.objects.create(
            branch=branch, product=product,
            quantity_on_hand=quantity, reorder_level=0,
        )

    def put_cart(self, lines):
        session = self.client.session
        session['cart'] = {str(p.pk): q for p, q in lines.items()}
        session.save()

    def share_location(self, latitude='14.951000', longitude='120.900000'):
        return self.client.post(reverse('orders:select_branch'), {
            'location_status': 'granted', 'latitude': latitude,
            'longitude': longitude, 'next': reverse('orders:catalog'),
        })

    def choose(self, branch, **extra):
        data = {'action': 'choose', 'branch_id': branch.pk}
        data.update(extra)
        return self.client.post(reverse('orders:select_branch'), data)

    def add(self, product, **extra):
        return self.client.post(
            reverse('orders:add_to_cart', args=[product.pk]), extra, follow=True
        )

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class ChoosingABranchTests(ManualBranchBase):
    def test_choosing_a_branch_records_it_as_a_manual_choice(self):
        self.stock(self.far, self.rice, 5)

        response = self.choose(self.far)

        self.assertEqual(self.client.session['branch_id'], self.far.pk)
        self.assertEqual(self.client.session['branch_mode'], 'manual')
        self.assertEqual(
            self.client.session['fulfillment_assignment']['strategy'], 'manual'
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('mode=manual', response['Location'])

    def test_a_chosen_branch_beats_a_nearer_one_when_items_are_added(self):
        """Automatic assignment would put this cart at Near Branch."""
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.share_location()
        self.choose(self.far)

        self.add(self.rice)

        self.assertEqual(self.client.session['branch_id'], self.far.pk)
        self.assertEqual(self.client.session['cart'], {str(self.rice.pk): 1})

    def test_a_short_branch_refuses_the_add_instead_of_moving_the_cart(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.near, self.oil, 5)
        self.stock(self.far, self.rice, 5)
        self.stock(self.far, self.oil, 0)
        self.choose(self.far)
        self.add(self.rice)

        response = self.add(self.oil)

        self.assertEqual(self.client.session['branch_id'], self.far.pk)
        self.assertEqual(self.client.session['cart'], {str(self.rice.pk): 1})
        joined = ' '.join(self.messages(response))
        self.assertIn('Far Branch cannot cover Oil', joined)
        self.assertIn('Your cart stays at Far Branch', joined)

    def test_choosing_a_branch_that_cannot_cover_the_cart_asks_first(self):
        self.stock(self.far, self.rice, 1)
        self.put_cart({self.rice: 2})

        response = self.choose(self.far)

        # Nothing moved: no branch, no manual mode, cart untouched.
        self.assertNotIn('branch_id', self.client.session)
        self.assertNotIn('branch_mode', self.client.session)
        self.assertEqual(self.client.session['cart'], {str(self.rice.pk): 2})
        self.assertIn(f'conflict={self.far.pk}', response['Location'])

        page = self.client.get(response['Location'])
        self.assertContains(page, "Far Branch can't cover your whole cart")
        self.assertContains(page, 'you have 2 in your cart, 1 left there')
        self.assertContains(page, 'Reduce quantities and switch')

    def test_reduce_trims_the_short_line_and_switches(self):
        self.stock(self.far, self.rice, 1)
        self.put_cart({self.rice: 2})

        self.choose(self.far, fix='reduce')

        self.assertEqual(self.client.session['cart'], {str(self.rice.pk): 1})
        self.assertEqual(self.client.session['branch_id'], self.far.pk)
        self.assertEqual(self.client.session['branch_mode'], 'manual')

    def test_reduce_drops_a_line_the_branch_has_none_of(self):
        self.stock(self.far, self.rice, 5)
        self.stock(self.far, self.oil, 0)
        self.put_cart({self.rice: 2, self.oil: 1})

        self.choose(self.far, fix='reduce')

        self.assertEqual(self.client.session['cart'], {str(self.rice.pk): 2})
        self.assertEqual(self.client.session['branch_id'], self.far.pk)

    def test_remove_drops_only_the_short_lines(self):
        self.stock(self.far, self.rice, 1)
        self.stock(self.far, self.oil, 9)
        self.put_cart({self.rice: 2, self.oil: 3})

        self.choose(self.far, fix='remove')

        self.assertEqual(self.client.session['cart'], {str(self.oil.pk): 3})
        self.assertEqual(self.client.session['branch_mode'], 'manual')

    def test_a_fix_is_ignored_when_the_branch_is_not_short(self):
        self.stock(self.far, self.rice, 9)
        self.put_cart({self.rice: 2})

        self.choose(self.far, fix='remove')

        self.assertEqual(self.client.session['cart'], {str(self.rice.pk): 2})

    def test_an_unknown_fix_value_changes_nothing(self):
        self.stock(self.far, self.rice, 1)
        self.put_cart({self.rice: 2})

        response = self.choose(self.far, fix='delete-everything')

        self.assertEqual(self.client.session['cart'], {str(self.rice.pk): 2})
        self.assertIn('conflict=', response['Location'])

    def test_a_closed_or_made_up_branch_cannot_be_chosen(self):
        closed = Branch.objects.create(
            branch_name='Closed', location='x', owner=self.owner, is_active=False,
        )

        for bad in (closed.pk, 999999, 'abc', ''):
            response = self.client.post(
                reverse('orders:select_branch'),
                {'action': 'choose', 'branch_id': bad},
                follow=True,
            )
            self.assertNotIn('branch_mode', self.client.session, bad)
            self.assertNotIn('branch_id', self.client.session, bad)
            self.assertIn(
                'That branch is not available. Choose one from the list.',
                self.messages(response),
            )

    def test_a_branch_id_without_the_choose_action_is_still_ignored(self):
        """Only action=choose reads branch_id; the old behaviour is intact."""
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.put_cart({self.rice: 1})

        self.client.post(reverse('orders:select_branch'), {
            'branch_id': self.far.pk, 'location_status': 'fallback',
        })

        self.assertNotIn('branch_mode', self.client.session)

    def test_a_branch_can_be_chosen_before_anything_is_in_the_cart(self):
        self.choose(self.far)

        self.assertEqual(self.client.session['branch_id'], self.far.pk)
        self.assertEqual(self.client.session['branch_mode'], 'manual')


class ManualChoiceIsNotSpoofableTests(ManualBranchBase):
    def test_checkout_ignores_a_posted_branch_id_in_manual_mode(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.share_location()
        self.choose(self.far)
        self.add(self.rice)
        self.client.get(reverse('orders:cart'))
        token = self.client.session['checkout_token']

        self.client.post(reverse('orders:checkout'), {
            'checkout_token': token,
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Pickup',
            'branch_id': self.near.pk,
        })

        self.assertEqual(SalesTransaction.objects.get().branch, self.far)

    def test_checkout_refuses_when_the_chosen_branch_ran_short(self):
        self.stock(self.near, self.rice, 5)
        far_stock = self.stock(self.far, self.rice, 5)
        self.choose(self.far)
        self.add(self.rice)
        self.client.get(reverse('orders:cart'))
        token = self.client.session['checkout_token']
        far_stock.quantity_on_hand = 0
        far_stock.save()

        response = self.client.post(reverse('orders:checkout'), {
            'checkout_token': token,
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Pickup',
        }, follow=True)

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn('Far Branch cannot cover Rice', ' '.join(self.messages(response)))

    def test_the_customer_sales_api_honours_the_chosen_branch(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.share_location()
        self.choose(self.far)

        response = self.client.post('/api/sales/', {
            'branchId': self.near.pk,
            'transactionType': SalesTransaction.TransactionType.ONLINE,
            'modeOfPayment': 'Cash on Delivery',
            'fulfillmentType': SalesTransaction.FulfillmentType.PICKUP,
            'items': [{'productId': self.rice.pk, 'quantity': 1}],
        }, content_type='application/json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(SalesTransaction.objects.get().branch, self.far)

    def test_the_customer_sales_api_refuses_a_short_chosen_branch(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 0)
        self.choose(self.far)

        response = self.client.post('/api/sales/', {
            'transactionType': SalesTransaction.TransactionType.ONLINE,
            'modeOfPayment': 'Cash on Delivery',
            'fulfillmentType': SalesTransaction.FulfillmentType.PICKUP,
            'items': [{'productId': self.rice.pk, 'quantity': 1}],
        }, content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Far Branch cannot fulfill', str(response.json()))
        self.assertEqual(SalesTransaction.objects.count(), 0)


class BackToAutomaticTests(ManualBranchBase):
    def test_the_automatic_action_ends_the_manual_choice(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.share_location()
        self.choose(self.far)
        self.put_cart({self.rice: 1})

        self.client.post(reverse('orders:select_branch'), {'action': 'automatic'})

        self.assertNotIn('branch_mode', self.client.session)
        # The location shared earlier is still used, so the nearer branch wins
        # again instead of the shopper being asked to share it a second time.
        self.assertIn('fulfillment_location', self.client.session)
        self.assertEqual(self.client.session['branch_id'], self.near.pk)

    def test_not_sharing_a_location_also_ends_the_manual_choice(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.choose(self.far)

        self.client.post(
            reverse('orders:select_branch'), {'location_status': 'fallback'}
        )

        self.assertNotIn('branch_mode', self.client.session)

    def test_sharing_a_location_ends_the_manual_choice(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.choose(self.far)

        self.share_location()

        self.assertNotIn('branch_mode', self.client.session)

    def test_a_chosen_branch_that_closes_falls_back_to_automatic_with_a_warning(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.choose(self.far)
        self.put_cart({self.rice: 1})
        self.far.is_active = False
        self.far.save()

        response = self.client.get(reverse('orders:cart'))

        self.assertNotIn('branch_mode', self.client.session)
        self.assertEqual(self.client.session['branch_id'], self.near.pk)
        self.assertIn(
            'The branch you chose is no longer available, so we switched to '
            'automatic branch selection.',
            self.messages(response),
        )


class BranchPickerPageTests(ManualBranchBase):
    def test_each_branch_says_whether_it_covers_the_cart(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 1)
        self.put_cart({self.rice: 2})

        page = self.client.get(reverse('orders:select_branch') + '?mode=manual')

        self.assertContains(page, 'Has all 1 item in your cart')
        self.assertContains(page, 'Short on Rice: you need 2, 1 left')
        self.assertContains(page, 'Shop here instead')

    def test_distances_follow_the_shared_location_and_nearest_comes_first(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        self.share_location()

        page = self.client.get(reverse('orders:select_branch') + '?mode=manual')

        body = page.content.decode()
        self.assertLess(body.index('Near Branch'), body.index('Far Branch'))
        self.assertContains(page, 'from your current location')

    def test_without_any_location_distances_are_hidden_not_invented(self):
        page = self.client.get(reverse('orders:select_branch') + '?mode=manual')

        self.assertNotContains(page, ' km from ')
        self.assertContains(page, 'Distances are hidden')

    def test_the_saved_address_pin_supplies_distances(self):
        Address.objects.create(
            user=self.customer, is_default=True, text='Home',
            latitude=Decimal('15.049000'), longitude=Decimal('120.900000'),
        )

        page = self.client.get(reverse('orders:select_branch') + '?mode=manual')

        self.assertContains(page, 'from your saved address')

    def test_inactive_branches_are_not_listed(self):
        Branch.objects.create(
            branch_name='Shuttered', location='x', owner=self.owner, is_active=False,
        )

        page = self.client.get(reverse('orders:select_branch') + '?mode=manual')

        self.assertNotContains(page, 'Shuttered')

    def test_the_page_opens_on_the_list_once_a_branch_was_chosen(self):
        self.choose(self.far)

        page = self.client.get(reverse('orders:select_branch'))

        self.assertContains(page, 'You are shopping at Far Branch')
        self.assertContains(page, 'Shopping here')

    def test_the_default_tab_is_still_nearest_to_me(self):
        page = self.client.get(reverse('orders:select_branch'))

        self.assertContains(page, 'Use my current location')
        self.assertContains(page, "Don't share my location")

    def test_header_strip_says_the_shopper_chose_it(self):
        self.stock(self.far, self.rice, 5)
        self.choose(self.far)

        # Not the catalog: it blanks the strip because its own rail has the
        # branch control. The product page keeps it.
        page = self.client.get(reverse('orders:product_detail', args=[self.rice.pk]))

        self.assertContains(page, 'Shopping at')
        self.assertContains(page, 'You chose this')
        self.assertContains(page, 'Change branch')

    def test_header_strip_offers_both_ways_when_automatic(self):
        self.stock(self.near, self.rice, 5)
        self.put_cart({self.rice: 1})
        # A cart with no branch bounces to the picker, so settle one the
        # automatic way first.
        self.client.post(reverse('orders:select_branch'), {'location_status': 'fallback'})

        page = self.client.get(reverse('orders:product_detail', args=[self.rice.pk]))

        self.assertContains(page, 'Fulfilled by')
        self.assertContains(page, 'Choose a branch')

    def test_the_cart_offers_the_way_back_to_automatic(self):
        self.stock(self.far, self.rice, 5)
        self.choose(self.far)
        self.put_cart({self.rice: 1})

        page = self.client.get(reverse('orders:cart'))

        self.assertContains(page, 'Shopping at <strong>Far Branch</strong>')
        self.assertContains(page, 'use automatic branch instead')
        self.assertNotContains(page, 'Location used:')


class ManualBranchServiceTests(ManualBranchBase):
    def test_resolve_manual_branch_covers_short_and_unavailable(self):
        self.stock(self.far, self.rice, 1)
        closed = Branch.objects.create(
            branch_name='Closed', location='x', owner=self.owner, is_active=False,
        )

        covered = resolve_manual_branch(self.far.pk, {str(self.rice.pk): 1})
        short = resolve_manual_branch(self.far.pk, {str(self.rice.pk): 2})
        gone = resolve_manual_branch(closed.pk, {str(self.rice.pk): 1})
        nothing = resolve_manual_branch(None, {})

        self.assertEqual((covered.strategy, covered.branch), ('manual', self.far))
        self.assertEqual(short.strategy, 'manual_short')
        self.assertIsNone(short.branch)
        self.assertEqual(short.chosen_branch, self.far)
        self.assertEqual(short.unavailable_product_ids, (self.rice.pk,))
        self.assertEqual(gone.strategy, 'manual_unavailable')
        self.assertEqual(nothing.strategy, 'manual_unavailable')

    def test_open_orders_count_against_a_chosen_branch_too(self):
        self.stock(self.far, self.rice, 3)
        other = User.objects.create_user(
            username='mb-other', password='pw', role=User.Role.CUSTOMER
        )
        from sales.models import SalesItem
        sale = SalesTransaction.objects.create(
            branch=self.far, customer=other,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PENDING,
        )
        SalesItem.objects.create(
            transaction=sale, product=self.rice, quantity_sold=2,
            unit_price=Decimal('100.00'),
        )

        resolution = resolve_manual_branch(self.far.pk, {str(self.rice.pk): 2})

        self.assertEqual(resolution.strategy, 'manual_short')

    def test_branch_options_orders_by_distance_then_name(self):
        self.stock(self.near, self.rice, 5)
        self.stock(self.far, self.rice, 5)
        nowhere = Branch.objects.create(
            branch_name='A Unlocated', location='x', owner=self.owner,
        )

        options = branch_options({str(self.rice.pk): 1}, Decimal('14.951'), Decimal('120.900'))

        self.assertEqual(
            [o['branch'].branch_name for o in options],
            ['Near Branch', 'Far Branch', 'A Unlocated'],
        )
        self.assertIsNone(options[-1]['distance_km'])
        self.assertTrue(options[0]['can_fulfill'])
        self.assertFalse(options[-1]['can_fulfill'])
        self.assertEqual(options[-1]['short'][0]['available'], 0)
