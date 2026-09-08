"""Online orders are priced by the same engine as the counter.

Two write paths reach a sale without a cashier: the storefront checkout
(orders.customer_views.checkout) and POST /api/sales/. Both used to get
pricing wrong, in opposite directions.

checkout() summed unit_price x qty by hand and wrote that as the total. No
VAT was ever broken out (total_vat_amount was 0 on every online order ever
placed), a wholesale customer could not be billed at wholesale, and there
was nowhere to record a PWD/Senior discount at all.

The API had the reverse problem: it ran the real pricing engine, but took
the caller's word for orderType. Any authenticated customer could post
orderType=Wholesale and be billed at wholesale_price.

Both now resolve the tier from the account — CustomerProfile.
is_wholesale_eligible, set by the Owner or a Branch Manager — and never from
the request. These tests hold that line, and check the online figures against
what the walk-in POS produces for the same cart.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class OnlinePricingTestCase(TestCase):
    """A branch, a VAT-inclusive product with a wholesale price, and stock."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        # ₱112.00 VAT-inclusive: the same figure core.tests_feature_update
        # prices the counter against, so the two surfaces can be compared
        # directly. 2 x ₱112.00 = ₱224.00 with ₱24.00 of VAT inside it;
        # with a Senior/PWD claim it becomes 2 x ₱80.00 = ₱160.00, VAT-exempt.
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
            wholesale_price=Decimal('89.60'),
            is_vat_exempt=False, is_pwd_eligible=True, is_senior_eligible=True,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )

    def customer(self, username='shopper', wholesale=False):
        """A CUSTOMER account. accounts.signals auto-creates the profile."""
        user = User.objects.create_user(
            username=username, password='pw', role=User.Role.CUSTOMER
        )
        if wholesale:
            profile = user.customer_profile
            profile.is_wholesale_eligible = True
            profile.save()
        return user

    def shop_as(self, user, quantity=2):
        """Log in and put `quantity` of the product in the session cart."""
        self.client.force_login(user)
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): quantity}
        session.save()

    def check_out(self, **extra):
        payload = {
            'mode_of_payment': 'Cash on Delivery',
            'delivery_address': '12 Real St, Baliuag',
        }
        payload.update(extra)
        return self.client.post(reverse('orders:checkout'), payload, follow=True)


class OnlineCheckoutPricingTests(OnlinePricingTestCase):

    def test_a_retail_order_records_the_same_vat_breakdown_as_the_counter(self):
        self.shop_as(self.customer())

        self.check_out()

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.order_type, SalesTransaction.OrderType.RETAIL)
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        # The whole point of B-2: this used to be 0.00 on every online order.
        self.assertEqual(sale.total_vat_amount, Decimal('24.00'))
        self.assertEqual(sale.total_discount_amount, Decimal('0.00'))

        item = sale.items.get()
        self.assertEqual(item.unit_price, Decimal('112.00'))
        self.assertEqual(item.vat_amount, Decimal('24.00'))
        self.assertEqual(item.discount_amount, Decimal('0.00'))
        self.assertFalse(item.is_vat_exempt_line)

    def test_an_online_retail_total_matches_the_walk_in_total_for_the_same_cart(self):
        """Parity, asserted against a real walk-in sale rather than a constant."""
        self.shop_as(self.customer())
        self.check_out()
        online = SalesTransaction.objects.get()

        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Retail', 'amount_tendered': '500',
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
        })
        walk_in = SalesTransaction.objects.exclude(pk=online.pk).get()

        self.assertEqual(online.total_amount, walk_in.total_amount)
        self.assertEqual(online.total_vat_amount, walk_in.total_vat_amount)
        self.assertEqual(
            online.items.get().unit_price, walk_in.items.get().unit_price
        )

    def test_an_eligible_customer_is_billed_at_the_wholesale_price(self):
        self.shop_as(self.customer(wholesale=True))

        self.check_out()

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.order_type, SalesTransaction.OrderType.WHOLESALE)
        self.assertEqual(sale.total_amount, Decimal('179.20'))   # 2 x ₱89.60
        self.assertEqual(sale.total_vat_amount, Decimal('19.20'))
        self.assertEqual(sale.items.get().unit_price, Decimal('89.60'))

    def test_an_ineligible_customer_gets_retail_even_if_wholesale_is_posted(self):
        """There is no order_type input on this form, and posting one anyway
        changes nothing — the tier comes from the account."""
        customer = self.customer()
        self.assertFalse(customer.customer_profile.is_wholesale_eligible)
        self.shop_as(customer)

        self.check_out(order_type='Wholesale')

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.order_type, SalesTransaction.OrderType.RETAIL)
        self.assertEqual(sale.total_amount, Decimal('224.00'))

    def test_the_eligibility_flag_is_what_drives_the_tier(self):
        """Same customer, same cart, twice — the only thing that changes is
        the flag, so the Retail result above cannot be an accident of there
        being no input to submit."""
        customer = self.customer()

        self.shop_as(customer)
        self.check_out()
        retail = SalesTransaction.objects.latest('pk')

        profile = customer.customer_profile
        profile.is_wholesale_eligible = True
        profile.save()

        self.shop_as(customer)
        self.check_out()
        wholesale = SalesTransaction.objects.latest('pk')

        self.assertNotEqual(retail.pk, wholesale.pk)
        self.assertEqual(retail.order_type, SalesTransaction.OrderType.RETAIL)
        self.assertEqual(retail.total_amount, Decimal('224.00'))
        self.assertEqual(wholesale.order_type, SalesTransaction.OrderType.WHOLESALE)
        self.assertEqual(wholesale.total_amount, Decimal('179.20'))

    def test_billing_now_receives_the_vat_correct_total(self):
        self.shop_as(self.customer())

        self.check_out()

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.billing_record.amount_due, sale.total_amount)


class OnlineDiscountClaimTests(OnlinePricingTestCase):

    def test_a_senior_claim_is_applied_and_the_line_goes_vat_exempt(self):
        self.shop_as(self.customer())

        self.check_out(
            discount_type='Senior',
            discount_id_number='OSCA-12345',
            discount_holder_name='Maria Santos',
        )

        sale = SalesTransaction.objects.get()
        # Identical to the counter's figures for this cart
        # (core.tests_feature_update.test_a_senior_discount_is_applied_and_recorded).
        self.assertEqual(sale.total_amount, Decimal('160.00'))       # 2 x ₱80.00
        self.assertEqual(sale.total_discount_amount, Decimal('40.00'))
        self.assertEqual(sale.total_vat_amount, Decimal('0.00'))
        self.assertEqual(sale.discount_type, 'Senior')
        self.assertEqual(sale.discount_id_number, 'OSCA-12345')
        self.assertEqual(sale.discount_holder_name, 'Maria Santos')

        item = sale.items.get()
        self.assertEqual(item.unit_price, Decimal('80.00'))
        self.assertEqual(item.discount_amount, Decimal('40.00'))
        self.assertTrue(item.is_vat_exempt_line)

    def test_an_online_discounted_total_matches_the_counter_for_the_same_cart(self):
        claim = {
            'discount_type': 'Senior',
            'discount_id_number': 'OSCA-12345',
            'discount_holder_name': 'Maria Santos',
        }
        self.shop_as(self.customer())
        self.check_out(**claim)
        online = SalesTransaction.objects.get()

        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_add'), dict(claim, **{
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'order_type': 'Retail', 'amount_tendered': '500',
            'product_id': [str(self.product.pk)], 'quantity': ['2'],
        }))
        walk_in = SalesTransaction.objects.exclude(pk=online.pk).get()

        self.assertEqual(online.total_amount, walk_in.total_amount)
        self.assertEqual(online.total_discount_amount, walk_in.total_discount_amount)
        self.assertEqual(online.total_vat_amount, walk_in.total_vat_amount)
        self.assertEqual(
            online.items.get().unit_price, walk_in.items.get().unit_price
        )

    def test_a_claim_without_an_id_number_is_refused_outright(self):
        self.shop_as(self.customer())

        response = self.check_out(
            discount_type='Senior', discount_holder_name='Maria Santos',
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn('ID number', response.content.decode())

    def test_a_claim_without_a_holder_name_is_refused_outright(self):
        self.shop_as(self.customer())

        response = self.check_out(
            discount_type='PWD', discount_id_number='PWD-999',
        )

        self.assertEqual(SalesTransaction.objects.count(), 0)
        self.assertIn('name on the PWD ID', response.content.decode())

    def test_both_missing_fields_are_reported_together(self):
        self.shop_as(self.customer())

        response = self.check_out(discount_type='PWD')

        self.assertEqual(SalesTransaction.objects.count(), 0)
        body = response.content.decode()
        self.assertIn('ID number', body)
        self.assertIn('name on the PWD ID', body)

    def test_an_unrecognised_claim_type_is_ignored_not_honoured(self):
        self.shop_as(self.customer())

        self.check_out(discount_type='Employee')

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        self.assertEqual(sale.total_discount_amount, Decimal('0.00'))


class SalesApiOrderTypeTests(OnlinePricingTestCase):
    """B-5: POST /api/sales/ honoured a client-submitted orderType outright."""

    def post_sale(self, user, **extra):
        self.client.force_login(user)
        payload = {
            'branchId': self.branch.pk,
            'transactionType': SalesTransaction.TransactionType.ONLINE,
            'modeOfPayment': 'Cash on Delivery',
            'deliveryAddress': '12 Real St, Baliuag',
            'items': [{'productId': self.product.pk, 'quantity': 2}],
        }
        payload.update(extra)
        return self.client.post(
            '/api/sales/', payload, content_type='application/json'
        )

    def test_an_ineligible_customer_asking_for_wholesale_is_priced_at_retail(self):
        response = self.post_sale(self.customer(), orderType='Wholesale')

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.order_type, SalesTransaction.OrderType.RETAIL)
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('112.00'))

    def test_an_eligible_customer_asking_for_wholesale_gets_it(self):
        """The flag makes wholesale legitimately reachable — the fix is a
        gate, not a blanket refusal."""
        response = self.post_sale(
            self.customer(wholesale=True), orderType='Wholesale'
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.order_type, SalesTransaction.OrderType.WHOLESALE)
        self.assertEqual(sale.total_amount, Decimal('179.20'))
        self.assertEqual(sale.items.get().unit_price, Decimal('89.60'))

    def test_a_customer_who_asks_for_nothing_still_gets_retail(self):
        response = self.post_sale(self.customer())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            SalesTransaction.objects.get().order_type,
            SalesTransaction.OrderType.RETAIL,
        )

    def test_the_discount_refusal_next_to_it_is_unchanged(self):
        """No discount is ever granted on an assertion over the wire, whatever
        the caller sends. This behaviour predates the fix and must survive it."""
        response = self.post_sale(
            self.customer(),
            discountType='Senior',
            discount_type='Senior',
            discount_id_number='OSCA-12345',
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        self.assertEqual(sale.total_discount_amount, Decimal('0.00'))
        self.assertEqual(sale.total_vat_amount, Decimal('24.00'))
        self.assertFalse(sale.items.get().is_vat_exempt_line)


class CartPreviewPricingTests(OnlinePricingTestCase):
    """B-6: the cart page priced every line off product.unit_price, so a
    wholesale customer read retail figures while browsing and then got
    wholesale totals at checkout."""

    def cart_page(self, user, quantity=2):
        self.shop_as(user, quantity=quantity)
        return self.client.get(reverse('orders:cart'))

    def test_an_eligible_customer_sees_wholesale_figures(self):
        response = self.cart_page(self.customer(wholesale=True))

        row = response.context['items'][0]
        self.assertEqual(row['unit_price'], Decimal('89.60'))
        self.assertEqual(row['subtotal'], Decimal('179.20'))
        self.assertEqual(response.context['total'], Decimal('179.20'))
        # The rendered "each" figure, not just the context — the template used
        # to read product.unit_price back off the product and undo the tier.
        self.assertContains(response, '89.60')
        self.assertNotContains(response, '112.00')

    def test_an_ineligible_customer_still_sees_retail(self):
        response = self.cart_page(self.customer())

        row = response.context['items'][0]
        self.assertEqual(row['unit_price'], Decimal('112.00'))
        self.assertEqual(row['subtotal'], Decimal('224.00'))
        self.assertEqual(response.context['total'], Decimal('224.00'))
        self.assertContains(response, '112.00')

    def test_the_free_delivery_meter_reads_off_the_corrected_total(self):
        """The meter is computed from `total`, so it has to move with the
        tier rather than being left on the retail figure."""
        retail = self.cart_page(self.customer('a'), quantity=4)
        wholesale = self.cart_page(self.customer('b', wholesale=True), quantity=4)

        self.assertEqual(retail.context['total'], Decimal('448.00'))
        self.assertEqual(wholesale.context['total'], Decimal('358.40'))
        # ₱448.00 clears the ₱500 bar by less than the retail figure suggests.
        self.assertEqual(
            wholesale.context['remaining_for_free'], Decimal('141.60')
        )
        self.assertGreater(
            wholesale.context['remaining_for_free'],
            retail.context['remaining_for_free'],
        )

    def test_the_cart_total_equals_what_checkout_charges(self):
        """Parity, for the same customer and the same cart. Asserted against a
        real checkout rather than a constant, so the two call sites have to
        actually agree instead of coincidentally matching a literal.

        Pre-discount on purpose: no PWD/Senior claim is made here because the
        cart page cannot know about one — it is typed at checkout.
        """
        for wholesale in (False, True):
            with self.subTest(wholesale=wholesale):
                customer = self.customer(
                    f'shopper{int(wholesale)}', wholesale=wholesale
                )

                previewed = self.cart_page(customer).context['total']

                self.shop_as(customer)
                self.check_out()
                sale = SalesTransaction.objects.latest('pk')

                self.assertEqual(previewed, sale.total_amount)
                self.assertEqual(
                    previewed, sale.billing_record.amount_due
                )

    def test_both_call_sites_derive_the_tier_from_one_function(self):
        """The eligibility check exists once. Flipping the flag has to move
        the cart preview and the written order together."""
        from orders.customer_views import order_type_for

        customer = self.customer()
        self.assertEqual(
            order_type_for(customer), SalesTransaction.OrderType.RETAIL
        )
        self.assertEqual(self.cart_page(customer).context['total'], Decimal('224.00'))

        profile = customer.customer_profile
        profile.is_wholesale_eligible = True
        profile.save()
        customer.refresh_from_db()

        self.assertEqual(
            order_type_for(customer), SalesTransaction.OrderType.WHOLESALE
        )
        self.assertEqual(self.cart_page(customer).context['total'], Decimal('179.20'))
