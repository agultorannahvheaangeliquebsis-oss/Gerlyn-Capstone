"""Online orders are priced by the same engine as the counter.

Two write paths reach a sale without a cashier: the storefront checkout
(orders.customer_views.checkout) and POST /api/sales/. Both used to get
pricing wrong, in opposite directions.

checkout() summed unit_price x qty by hand and wrote that as the total. No
VAT was ever broken out (total_vat_amount was 0 on every online order ever
placed).

(It briefly grew a PWD/Senior claim here too. F-04 took it back out — the
discount is granted by staff against an ID they have seen, in the Order
Console; see sales/tests_verified_discount.py.)

The API had the reverse problem: it ran the real pricing engine, but took
the caller's word for the price tier. Any authenticated customer could post
orderType=Wholesale and be billed at the wholesale price.

There is no longer a tier to ask for. Bulk pricing is a property of the
product and the quantity bought — Product.price_at walks the product's
ProductPriceTier ladder — so every path, counter included, reaches the same
price for the same cart and nothing in a request can move it. These tests
hold that line, and check the online figures against what the walk-in POS
produces for the same cart.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product, StockMovement
from sales.models import SalesTransaction


# The fixture's single bulk break. Below it a line pays retail; at or above it,
# the tier price.
BULK_QUANTITY = 10


class OnlinePricingTestCase(TestCase):
    """A branch, a VAT-inclusive product with one bulk tier, and stock."""

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
        #
        # The bulk tier is 10 or more at ₱89.60, which keeps the VAT exact:
        # 10 x ₱89.60 = ₱896.00 with ₱96.00 of VAT inside it.
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
            is_vat_exempt=False, is_pwd_eligible=True, is_senior_eligible=True,
        )
        self.product.price_tiers.create(
            min_quantity=BULK_QUANTITY, unit_price=Decimal('89.60')
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )

    def customer(self, username='shopper'):
        """A CUSTOMER account. accounts.signals auto-creates the profile."""
        return User.objects.create_user(
            username=username, password='pw', role=User.Role.CUSTOMER
        )

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
            # fulfillment_type is required since F-11 — checkout refuses a
            # POST that does not say delivery or pickup. Pickup here so these
            # pricing tests need no saved Address (a Delivery order is refused
            # without one); the price engine does not care which it is.
            'fulfillment_type': 'Pickup',
        }
        payload.update(extra)
        return self.client.post(reverse('orders:checkout'), payload, follow=True)

    def walk_in(self, quantity):
        """Ring the same product up at the counter and return the sale."""
        self.client.force_login(self.owner)
        self.client.post(reverse('transaction_add'), {
            'branch': self.branch.pk, 'mode_of_payment': 'Cash',
            'amount_tendered': '5000',
            'product_id': [str(self.product.pk)], 'quantity': [str(quantity)],
        })
        return SalesTransaction.objects.filter(
            transaction_type=SalesTransaction.TransactionType.PHYSICAL
        ).latest('pk')


class OnlineCheckoutPricingTests(OnlinePricingTestCase):

    def test_a_retail_order_records_the_same_vat_breakdown_as_the_counter(self):
        self.shop_as(self.customer())

        self.check_out()

        sale = SalesTransaction.objects.get()
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

        walk_in = self.walk_in(quantity=2)

        self.assertEqual(online.total_amount, walk_in.total_amount)
        self.assertEqual(online.total_vat_amount, walk_in.total_vat_amount)
        self.assertEqual(
            online.items.get().unit_price, walk_in.items.get().unit_price
        )

    def test_a_bulk_quantity_is_billed_at_the_tier_price(self):
        self.shop_as(self.customer(), quantity=BULK_QUANTITY)

        self.check_out()

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.total_amount, Decimal('896.00'))   # 10 x ₱89.60
        self.assertEqual(sale.total_vat_amount, Decimal('96.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('89.60'))

    def test_an_online_bulk_total_matches_the_walk_in_total_for_the_same_cart(self):
        """The tier is read by the same rule on both surfaces, so a bulk cart
        cannot be cheaper at the counter than online, or the other way round."""
        self.shop_as(self.customer(), quantity=BULK_QUANTITY)
        self.check_out()
        online = SalesTransaction.objects.get()

        walk_in = self.walk_in(quantity=BULK_QUANTITY)

        self.assertEqual(online.total_amount, walk_in.total_amount)
        self.assertEqual(online.total_vat_amount, walk_in.total_vat_amount)
        self.assertEqual(
            online.items.get().unit_price, walk_in.items.get().unit_price
        )

    def test_posting_a_wholesale_order_type_changes_nothing(self):
        """The Retail/Wholesale tier used to be a field. A stale client still
        posting one must not reach a bulk price its quantity has not earned."""
        self.shop_as(self.customer(), quantity=2)

        self.check_out(order_type='Wholesale')

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('112.00'))

    def test_billing_now_receives_the_vat_correct_total(self):
        self.shop_as(self.customer())

        self.check_out()

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.billing_record.amount_due, sale.total_amount)


# The PWD/Senior claim used to be made HERE, on the checkout form, and this
# module carried six tests pinning that behaviour: a Senior claim applied and
# went VAT-exempt, an online discounted total matched the counter's, a claim
# missing its ID number or holder name was refused, and an unrecognised claim
# type fell back to no discount.
#
# F-04 removed the whole surface. The guard those tests described — that the
# ID fields be non-empty strings — was never verification: 'MADE-UP-123'
# satisfied it, and no staff member ever saw a card. Checkout now ignores
# discount_type entirely, so there is nothing left here to assert about it
# beyond "it is ignored", and that lives with the rest of the F-04 coverage
# in sales/tests_verified_discount.py, next to the Order Console action that
# replaced it.
#
# The parity these tests were really protecting — that an online discounted
# total matches the counter's for the same cart — is still asserted, in
# ConsoleDiscountAllowedTests.test_a_pickup_order_is_discounted_and_recalculated
# against the same ₱112.00 fixture and the same expected ₱160.00.


class SalesApiPricingTests(OnlinePricingTestCase):
    """B-5: POST /api/sales/ honoured a client-submitted orderType outright.
    The field is gone; the price comes from the quantity ordered."""

    def post_sale(self, user, items=None, **extra):
        self.client.force_login(user)
        payload = {
            'branchId': self.branch.pk,
            'transactionType': SalesTransaction.TransactionType.ONLINE,
            'modeOfPayment': 'Cash on Delivery',
            'deliveryAddress': '12 Real St, Baliuag',
            'items': items or [{'productId': self.product.pk, 'quantity': 2}],
        }
        payload.update(extra)
        return self.client.post(
            '/api/sales/', payload, content_type='application/json'
        )

    def test_a_quantity_below_the_tier_is_priced_at_retail(self):
        response = self.post_sale(self.customer())

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('112.00'))

    def test_a_quantity_at_the_tier_is_priced_at_the_tier(self):
        response = self.post_sale(
            self.customer(),
            items=[{'productId': self.product.pk, 'quantity': BULK_QUANTITY}],
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.total_amount, Decimal('896.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('89.60'))

    def test_asking_for_wholesale_over_the_wire_changes_nothing(self):
        response = self.post_sale(self.customer(), orderType='Wholesale')

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        self.assertEqual(sale.items.get().unit_price, Decimal('112.00'))

    def test_splitting_one_product_across_two_items_still_earns_the_break(self):
        """5 + 5 of the same product is a 10-unit purchase. Pricing each item
        on its own would let a client dodge the tier — or a store under-apply
        it — just by how the payload happened to be shaped."""
        response = self.post_sale(
            self.customer(),
            items=[
                {'productId': self.product.pk, 'quantity': 5},
                {'productId': self.product.pk, 'quantity': 5},
            ],
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(
            sorted(item.unit_price for item in sale.items.all()),
            [Decimal('89.60'), Decimal('89.60')],
        )
        self.assertEqual(sale.total_amount, Decimal('896.00'))

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


class SalesApiOnlineStockDeductionTests(OnlinePricingTestCase):
    """F-01: SalesTransactionWriteSerializer.create() deducted stock for
    every channel at creation time, including Online. transaction_set_status
    then deducted it AGAIN when the Order Console walked that same order to
    Completed — a 5-unit online order dropped 10 units of stock.

    Stock must move exactly once, at Completion, matching the storefront
    checkout path (orders.customer_views.checkout) and
    core.tests.test_online_order_deducts_stock_only_once_on_completion,
    which pins the same rule for that other write path.
    """

    def post_sale(self, user, **extra):
        self.client.force_login(user)
        payload = {
            'branchId': self.branch.pk,
            'transactionType': SalesTransaction.TransactionType.ONLINE,
            'modeOfPayment': 'Cash on Delivery',
            'deliveryAddress': '12 Real St, Baliuag',
            'items': [{'productId': self.product.pk, 'quantity': 5}],
        }
        payload.update(extra)
        return self.client.post(
            '/api/sales/', payload, content_type='application/json'
        )

    def test_an_online_order_does_not_deduct_stock_at_creation(self):
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        before = inv.quantity_on_hand

        response = self.post_sale(self.customer())

        self.assertEqual(response.status_code, 201)
        inv.refresh_from_db()
        self.assertEqual(inv.quantity_on_hand, before, 'stock must not move at creation')
        self.assertFalse(
            StockMovement.objects.exists(),
            'no movement should be logged until the order is actually fulfilled',
        )

    def test_walking_the_order_to_completed_deducts_exactly_once(self):
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        before = inv.quantity_on_hand

        response = self.post_sale(self.customer())
        order = SalesTransaction.objects.get(pk=response.data['id'])

        self.client.force_login(self.owner)
        for status in ['Approved', 'Preparing', 'Completed']:
            resp = self.client.post(
                reverse('transaction_set_status', args=[order.pk, status])
            )
            self.assertEqual(resp.status_code, 302)

        order.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(order.order_status, 'Completed')
        # Exactly the 5 ordered units — once, not the 10 the double
        # deduction used to take.
        self.assertEqual(inv.quantity_on_hand, before - 5)

        movement = StockMovement.objects.get()
        self.assertEqual(movement.quantity_change, -5)
        self.assertEqual(movement.reference_id, order.pk)
        self.assertEqual(movement.movement_type, StockMovement.MovementType.SALE)

    def test_a_physical_sale_still_deducts_at_creation_and_now_logs_it(self):
        """Physical IS the point of sale for this endpoint — there is no
        console step to wait for — so it keeps deducting immediately. It
        previously did so with no StockMovement row; this pins that it now
        gets one, same as the walk-in POS and the Console's own deduction."""
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        before = inv.quantity_on_hand

        response = self.post_sale(
            self.owner, transactionType=SalesTransaction.TransactionType.PHYSICAL,
            deliveryAddress='',
        )

        self.assertEqual(response.status_code, 201)
        order = SalesTransaction.objects.get(pk=response.data['id'])
        inv.refresh_from_db()
        self.assertEqual(inv.quantity_on_hand, before - 5)

        movement = StockMovement.objects.get()
        self.assertEqual(movement.quantity_change, -5)
        self.assertEqual(movement.reference_id, order.pk)
        self.assertEqual(movement.movement_type, StockMovement.MovementType.SALE)


class CartPreviewPricingTests(OnlinePricingTestCase):
    """B-6: the cart page priced every line off product.unit_price, so a
    shopper due a lower price read one figure on the cart and was billed
    another at checkout. The cart now prices each line at the tier its
    quantity earns, the same way checkout does."""

    def cart_page(self, user, quantity=2):
        self.shop_as(user, quantity=quantity)
        return self.client.get(reverse('orders:cart'))

    def test_a_bulk_quantity_shows_the_tier_figures(self):
        response = self.cart_page(self.customer(), quantity=BULK_QUANTITY)

        row = response.context['items'][0]
        self.assertEqual(row['unit_price'], Decimal('89.60'))
        self.assertEqual(row['subtotal'], Decimal('896.00'))
        self.assertEqual(response.context['total'], Decimal('896.00'))
        # The rendered "each" figure, not just the context — the template used
        # to read product.unit_price back off the product and undo the tier.
        self.assertContains(response, '89.60')
        self.assertNotContains(response, '112.00')

    def test_a_quantity_below_the_tier_still_shows_retail(self):
        response = self.cart_page(self.customer())

        row = response.context['items'][0]
        self.assertEqual(row['unit_price'], Decimal('112.00'))
        self.assertEqual(row['subtotal'], Decimal('224.00'))
        self.assertEqual(response.context['total'], Decimal('224.00'))
        self.assertContains(response, '112.00')

    def test_the_free_delivery_meter_reads_off_the_corrected_total(self):
        """The meter is computed from `total`, so it has to move with the
        tier rather than being left on the retail figure.

        A shallower break at 4 units is added so the tier lands below the
        ₱500 free-delivery bar: retail would read 4 x ₱112.00 = ₱448.00
        (₱52.00 to go), the tier reads 4 x ₱100.00 = ₱400.00 (₱100.00 to go).
        """
        self.product.price_tiers.create(min_quantity=4, unit_price=Decimal('100.00'))

        response = self.cart_page(self.customer(), quantity=4)

        self.assertEqual(response.context['total'], Decimal('400.00'))
        self.assertEqual(
            response.context['remaining_for_free'], Decimal('100.00')
        )

    def test_the_cart_total_equals_what_checkout_charges(self):
        """Parity, for the same customer and the same cart. Asserted against a
        real checkout rather than a constant, so the two call sites have to
        actually agree instead of coincidentally matching a literal.

        Pre-discount on purpose: no PWD/Senior claim is made here because the
        cart page cannot know about one — it is granted after the order exists.
        """
        for quantity in (2, BULK_QUANTITY):
            with self.subTest(quantity=quantity):
                customer = self.customer(f'shopper{quantity}')

                previewed = self.cart_page(customer, quantity=quantity).context['total']

                self.shop_as(customer, quantity=quantity)
                self.check_out()
                sale = SalesTransaction.objects.latest('pk')

                self.assertEqual(previewed, sale.total_amount)
                self.assertEqual(
                    previewed, sale.billing_record.amount_due
                )

    def test_editing_the_ladder_moves_the_cart_and_checkout_together(self):
        """Both call sites read the product's tiers through Product.price_at,
        so a price change in the product form has to reach the preview and
        the written order at the same moment — neither can hold a stale copy."""
        customer = self.customer()
        self.assertEqual(
            self.cart_page(customer, quantity=BULK_QUANTITY).context['total'],
            Decimal('896.00'),
        )

        self.product.price_tiers.filter(min_quantity=BULK_QUANTITY).update(
            unit_price=Decimal('80.00')
        )

        self.assertEqual(
            self.cart_page(customer, quantity=BULK_QUANTITY).context['total'],
            Decimal('800.00'),
        )
        self.shop_as(customer, quantity=BULK_QUANTITY)
        self.check_out()
        self.assertEqual(
            SalesTransaction.objects.latest('pk').total_amount, Decimal('800.00')
        )


class OnlineCheckoutPaymentMethodTests(OnlinePricingTestCase):
    """F-05 — checkout() wrote request.POST['mode_of_payment'] onto the sale
    and the billing record with no check against ModeOfPayment.choices, while
    discount_type right beside it falls back to a safe default on an
    unrecognised value. A tampered form could stamp 'FREE MONEY' onto both
    rows and break the Billing report's payment breakdown. Same fix: fall
    back to the online default (Cash on Delivery).
    """

    def test_a_bogus_payment_method_falls_back_to_the_online_default(self):
        self.shop_as(self.customer())
        self.check_out(mode_of_payment='FREE MONEY')

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.mode_of_payment, SalesTransaction.ModeOfPayment.COD)
        self.assertNotEqual(sale.mode_of_payment, 'FREE MONEY')
        # The billing record is written from the same value, so it is clean too.
        self.assertEqual(sale.billing_record.payment_method, 'Cash on Delivery')

    def test_a_valid_payment_method_is_kept(self):
        # Guards against the check being blunt enough to rewrite every order to
        # the default — 'Pay at Store' is a real ModeOfPayment choice.
        self.shop_as(self.customer())
        self.check_out(mode_of_payment=SalesTransaction.ModeOfPayment.PAY_AT_STORE)

        sale = SalesTransaction.objects.get()
        self.assertEqual(
            sale.mode_of_payment, SalesTransaction.ModeOfPayment.PAY_AT_STORE
        )
        self.assertEqual(sale.billing_record.payment_method, 'Pay at Store')
