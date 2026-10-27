"""The statutory discount is granted by staff against a sighted ID, never claimed.

F-04 — orders.customer_views.checkout used to read `discount_type` from the
POST and apply the full RA 10754 / RA 9994 treatment on it: 20% off the
VAT-exclusive price plus VAT exemption. The only guard was that the ID-number
and holder-name fields be non-empty strings, so a made-up value bought a
permanent reduction and no staff member ever saw a card. The customer-facing
form did not even offer the field — it was reachable only by hand-crafting
the request, which made it purely an attack surface.

Checkout now ignores the field entirely, and the discount moves to the Order
Console, where a named person applies it at the moment they verify the ID.
That is possible in exactly two situations, and impossible in a third:

  * a walk-in (Physical) sale at the counter          -> allowed
  * an Online order collected in store (Pickup)       -> allowed
  * an Online order being delivered                   -> refused

because a Delivery order has no point at which anyone at the branch meets
the cardholder.
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import Address, User
from billing.models import BillingRecord
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class DiscountFixtureMixin:
    """A branch, an eligible ₱112.00 VAT-inclusive product, and stock.

    ₱112.00 is the figure core.tests_feature_update and
    sales.tests_online_pricing both price against, so the numbers here can be
    compared directly with the counter's. 2 x ₱112.00 = ₱224.00 with ₱24.00
    of VAT inside; discounted it is 2 x ₱80.00 = ₱160.00, VAT-exempt.
    """

    def build_fixture(self):
        self.owner = User.objects.create_user(
            username='vd-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-VD', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('112.00'),
            is_vat_exempt=False, is_pwd_eligible=True, is_senior_eligible=True,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )
        self.shopper = User.objects.create_user(
            username='vd-shopper', password='pw', role=User.Role.CUSTOMER
        )
        # A Delivery checkout ships to the account's default Address and is
        # refused without one — every checkout in this file that chooses
        # Delivery relies on this being here.
        Address.objects.create(
            user=self.shopper, is_default=True, text='12 Real St, Baliuag',
        )

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class CheckoutIgnoresDiscountClaimTests(DiscountFixtureMixin, TestCase):
    """The audit's own repro: a crafted checkout POST claiming a discount."""

    def setUp(self):
        self.build_fixture()
        self.client.force_login(self.shopper)
        session = self.client.session
        session['branch_id'] = self.branch.pk
        session['cart'] = {str(self.product.pk): 2}
        session['checkout_token'] = 'test-checkout-token'
        session.save()

    def check_out(self, **extra):
        payload = {
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Delivery',
            'delivery_address': '12 Real St, Baliuag',
            'addr_contact': '0917 123 4567',
            'checkout_token': 'test-checkout-token',
        }
        payload.update(extra)
        return self.client.post(
            reverse('orders:checkout'), payload, follow=True
        )

    def test_a_made_up_id_number_buys_no_discount(self):
        """The finding, verbatim. This used to take ₱64.00 off the order on
        the strength of a string nobody checked."""
        self.check_out(
            discount_type='Senior',
            discount_id_number='MADE-UP-123',
            discount_holder_name='Not A Real Person',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertEqual(sale.total_discount_amount, Decimal('0.00'))
        # Full price, with the VAT still recorded inside it.
        self.assertEqual(sale.total_amount, Decimal('224.00'))
        self.assertEqual(sale.total_vat_amount, Decimal('24.00'))

        item = sale.items.get()
        self.assertEqual(item.unit_price, Decimal('112.00'))
        self.assertEqual(item.discount_amount, Decimal('0.00'))
        self.assertFalse(item.is_vat_exempt_line)

    def test_the_claim_is_ignored_silently_not_rejected(self):
        """The order still goes through. The field is not part of this form,
        so a request carrying one is not an error to report to the shopper —
        it is simply not read."""
        response = self.check_out(
            discount_type='PWD',
            discount_id_number='MADE-UP-123',
            discount_holder_name='Not A Real Person',
        )

        self.assertEqual(SalesTransaction.objects.count(), 1)
        body = response.content.decode()
        self.assertIn('placed!', body)
        for message in self.messages(response):
            self.assertNotIn('ID number', message)

    def test_the_id_fields_are_not_even_recorded(self):
        """Storing the strings without applying a discount would still leave
        an unverified name and ID number on the order, reading as though
        someone had checked something."""
        self.check_out(
            discount_type='Senior',
            discount_id_number='MADE-UP-123',
            discount_holder_name='Not A Real Person',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.discount_id_number, '')
        self.assertEqual(sale.discount_holder_name, '')
        self.assertIsNone(sale.discount_applied_by)
        self.assertIsNone(sale.discount_applied_at)

    def test_a_pickup_order_is_not_discounted_at_checkout_either(self):
        """Pickup is the case that CAN be discounted later — but not here,
        and not by the customer. Staff apply it when the ID is presented."""
        self.check_out(
            fulfillment_type='Pickup', delivery_address='',
            discount_type='Senior',
            discount_id_number='MADE-UP-123',
            discount_holder_name='Not A Real Person',
        )

        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertEqual(sale.total_amount, Decimal('224.00'))

    def test_the_checkout_form_offers_no_discount_field(self):
        """Nothing on the customer-facing page invites the claim."""
        body = self.client.get(reverse('orders:cart')).content.decode()

        self.assertNotIn('discount_type', body)
        self.assertNotIn('discount_id_number', body)
        self.assertNotIn('discount_holder_name', body)


class ConsoleDiscountTestCase(DiscountFixtureMixin, TestCase):
    """Shared setup for the Order Console action."""

    def setUp(self):
        self.build_fixture()
        # accounts.signals creates the profile; the branch lives on it, not
        # on User. A branchless Staff account is refused by branch scoping
        # before the view runs (F-13), so this has to be set.
        self.staff = User.objects.create_user(
            username='vd-staff', password='pw', role=User.Role.STAFF
        )
        self.staff.staff_profile.branch = self.branch
        self.staff.staff_profile.save()

    def make_order(self, fulfillment_type='Pickup',
                   transaction_type='Online', **extra):
        """An undiscounted 2-line order, priced the way checkout writes one."""
        extra.setdefault(
            'customer', self.shopper if transaction_type == 'Online' else None
        )
        extra.setdefault(
            'delivery_address',
            '12 Real St, Baliuag' if fulfillment_type == 'Delivery' else '',
        )
        extra.setdefault('order_status', SalesTransaction.OrderStatus.PENDING)
        extra.setdefault('mode_of_payment', 'Cash on Delivery')
        extra.setdefault('total_amount', Decimal('224.00'))
        extra.setdefault('total_vat_amount', Decimal('24.00'))
        txn = SalesTransaction.objects.create(
            branch=self.branch,
            transaction_type=transaction_type,
            fulfillment_type=fulfillment_type,
            **extra,
        )
        txn.items.create(
            product=self.product, quantity_sold=2,
            unit_price=Decimal('112.00'), vat_amount=Decimal('24.00'),
            is_vat_exempt_line=False,
        )
        return txn

    def apply(self, txn, user=None, **extra):
        self.client.force_login(user or self.owner)
        payload = {
            'discount_type': 'Senior',
            'discount_id_number': 'OSCA-12345',
            'discount_holder_name': 'Maria Santos',
        }
        payload.update(extra)
        return self.client.post(
            reverse('transaction_apply_discount', args=[txn.pk]),
            payload, follow=True,
        )


class ConsoleDiscountBlockedOnDeliveryTests(ConsoleDiscountTestCase):
    """The gate. A delivery order has no in-person verification moment."""

    def test_a_delivery_order_is_refused(self):
        txn = self.make_order(fulfillment_type='Delivery')

        response = self.apply(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertEqual(txn.total_amount, Decimal('224.00'))
        self.assertEqual(txn.total_discount_amount, Decimal('0.00'))
        self.assertIsNone(txn.discount_applied_by)
        self.assertIn(
            f'{txn.order_number} is for delivery — a PWD / Senior Citizen '
            f'discount can only be applied in person, at the counter for a '
            f'walk-in or when a pickup order is collected.',
            self.messages(response),
        )

    def test_the_delivery_order_line_is_left_untouched(self):
        """The refusal happens before any re-pricing, so the frozen line
        keeps its original price and VAT."""
        txn = self.make_order(fulfillment_type='Delivery')

        self.apply(txn)

        item = txn.items.get()
        self.assertEqual(item.unit_price, Decimal('112.00'))
        self.assertEqual(item.discount_amount, Decimal('0.00'))
        self.assertFalse(item.is_vat_exempt_line)

    def test_the_console_does_not_offer_the_control_on_a_delivery_order(self):
        txn = self.make_order(fulfillment_type='Delivery')
        self.client.force_login(self.owner)

        body = self.client.get(
            reverse('transaction_detail', args=[txn.pk])
        ).content.decode()

        self.assertNotIn('Apply PWD / Senior Citizen discount', body)

    def test_a_cancelled_pickup_order_is_refused(self):
        txn = self.make_order(
            order_status=SalesTransaction.OrderStatus.CANCELLED
        )

        response = self.apply(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertIn(
            f'{txn.order_number} is cancelled — there is nothing to discount.',
            self.messages(response),
        )

    def test_an_already_discounted_order_is_not_discounted_twice(self):
        """Re-pricing discounted lines would take 20% off the discounted
        price and compound the reduction."""
        txn = self.make_order()
        self.apply(txn)
        txn.refresh_from_db()
        first_total = txn.total_amount

        response = self.apply(txn, discount_type='PWD',
                              discount_id_number='PWD-999')

        txn.refresh_from_db()
        self.assertEqual(txn.total_amount, first_total)
        self.assertEqual(txn.discount_type, 'Senior')
        self.assertIn(
            f'{txn.order_number} already carries a Senior Citizen discount.',
            self.messages(response),
        )

    def test_a_paid_order_is_refused(self):
        """The amount due is settled — changing the total now would leave the
        payment and the bill disagreeing."""
        txn = self.make_order()
        billing = txn.billing_record
        billing.status = BillingRecord.PaymentStatus.PAID
        billing.save()

        response = self.apply(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertIn(
            f'{txn.order_number} is already paid — the amount due cannot be '
            f'changed. Reverse the payment first if this discount is owed.',
            self.messages(response),
        )

    def test_another_branch_cannot_reach_the_order(self):
        """The branch scoping every other console action enforces."""
        other_branch = Branch.objects.create(
            branch_name='Other', location='Elsewhere', owner=self.owner
        )
        outsider = User.objects.create_user(
            username='vd-outsider', password='pw',
            role=User.Role.BRANCH_MANAGER,
        )
        outsider.manager_profile.branch = other_branch
        outsider.manager_profile.save()
        txn = self.make_order()

        response = self.apply(txn, user=outsider)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertIn(
            'That order belongs to another branch.', self.messages(response)
        )

    def test_a_customer_cannot_apply_a_discount(self):
        """The action is staff-only — role_required, same as every other
        lifecycle action on the console."""
        txn = self.make_order()

        response = self.apply(txn, user=self.shopper)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertEqual(txn.total_amount, Decimal('224.00'))

    def test_a_get_request_changes_nothing(self):
        txn = self.make_order()
        self.client.force_login(self.owner)

        self.client.get(reverse('transaction_apply_discount', args=[txn.pk]))

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)


class ConsoleDiscountAllowedTests(ConsoleDiscountTestCase):
    """Pickup and walk-in: the two cases where staff see the ID."""

    def test_a_pickup_order_is_discounted_and_recalculated(self):
        txn = self.make_order(fulfillment_type='Pickup')

        response = self.apply(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, 'Senior')
        self.assertEqual(txn.discount_id_number, 'OSCA-12345')
        self.assertEqual(txn.discount_holder_name, 'Maria Santos')
        # The pricing engine's figures, identical to the counter's for this
        # cart: VAT stripped first (112 / 1.12 = 100), then 20% off.
        self.assertEqual(txn.total_amount, Decimal('160.00'))
        self.assertEqual(txn.total_discount_amount, Decimal('40.00'))
        self.assertEqual(txn.total_vat_amount, Decimal('0.00'))
        self.assertIn(
            f'Senior discount applied to {txn.order_number} — ₱40.00 off, '
            f'now ₱160.00.',
            self.messages(response),
        )

    def test_the_line_items_are_re_priced_through_the_engine(self):
        txn = self.make_order(fulfillment_type='Pickup')

        self.apply(txn)

        item = txn.items.get()
        self.assertEqual(item.unit_price, Decimal('80.00'))
        self.assertEqual(item.subtotal, Decimal('160.00'))
        self.assertEqual(item.discount_amount, Decimal('40.00'))
        self.assertEqual(item.vat_amount, Decimal('0.00'))
        self.assertTrue(item.is_vat_exempt_line)

    def test_a_walk_in_sale_is_discounted(self):
        """A Physical sale qualifies whatever its fulfillment_type — the
        customer is standing at the counter."""
        txn = self.make_order(
            transaction_type='Physical', fulfillment_type='Pickup',
            staff=self.staff, mode_of_payment='Cash',
        )

        self.apply(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, 'Senior')
        self.assertEqual(txn.total_amount, Decimal('160.00'))

    def test_it_logs_who_applied_it_and_when(self):
        """The A-1 audit trail: a 20% reduction with nobody's name against
        it is exactly what the next audit round asks about."""
        txn = self.make_order(fulfillment_type='Pickup')

        self.apply(txn, user=self.staff)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_applied_by, self.staff)
        self.assertIsNotNone(txn.discount_applied_at)

    def test_the_billing_record_follows_the_new_total(self):
        """Otherwise the customer is asked to pay the pre-discount amount."""
        txn = self.make_order(fulfillment_type='Pickup')

        self.apply(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.billing_record.amount_due, Decimal('160.00'))
        self.assertEqual(txn.billing_record.amount_due, txn.total_amount)

    def test_a_pwd_claim_works_the_same_way(self):
        txn = self.make_order(fulfillment_type='Pickup')

        self.apply(txn, discount_type='PWD', discount_id_number='PWD-999')

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, 'PWD')
        self.assertEqual(txn.discount_id_number, 'PWD-999')
        self.assertEqual(txn.total_amount, Decimal('160.00'))

    def test_an_ineligible_product_records_the_claim_but_moves_no_money(self):
        """PWD and Senior are separate statutes with separate exclusion
        lists. price_cart decides per line; the view must not pretend a
        discount was given when none applied."""
        self.product.is_senior_eligible = False
        self.product.save()
        txn = self.make_order(fulfillment_type='Pickup')

        response = self.apply(txn)

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, 'Senior')
        self.assertEqual(txn.total_amount, Decimal('224.00'))
        self.assertEqual(txn.total_discount_amount, Decimal('0.00'))
        self.assertIn(
            f'Senior recorded on {txn.order_number}, but no item on it is '
            f'eligible for that discount — the total is unchanged.',
            self.messages(response),
        )

    def test_a_claim_without_an_id_number_is_refused(self):
        txn = self.make_order(fulfillment_type='Pickup')

        response = self.apply(txn, discount_id_number='')

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertIn(
            'Enter the Senior ID number — the discount is conditioned on one '
            'being presented.',
            self.messages(response),
        )

    def test_a_claim_without_a_holder_name_is_refused(self):
        txn = self.make_order(fulfillment_type='Pickup')

        response = self.apply(txn, discount_holder_name='')

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertIn(
            'Enter the name on the Senior ID.', self.messages(response)
        )

    def test_an_unrecognised_discount_type_is_refused(self):
        txn = self.make_order(fulfillment_type='Pickup')

        response = self.apply(txn, discount_type='Employee')

        txn.refresh_from_db()
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertEqual(txn.total_amount, Decimal('224.00'))
        self.assertIn('Choose PWD or Senior Citizen.', self.messages(response))

    def test_none_is_not_an_applicable_discount(self):
        """'Apply no discount' is not an action, and must not write an audit
        trail for a no-op."""
        txn = self.make_order(fulfillment_type='Pickup')

        self.apply(txn, discount_type='None')

        txn.refresh_from_db()
        self.assertIsNone(txn.discount_applied_by)
        self.assertEqual(txn.total_amount, Decimal('224.00'))

    def test_the_console_offers_the_control_on_a_pickup_order(self):
        txn = self.make_order(fulfillment_type='Pickup')
        self.client.force_login(self.owner)

        body = self.client.get(
            reverse('transaction_detail', args=[txn.pk])
        ).content.decode()

        self.assertIn('Apply PWD / Senior Citizen discount', body)

    def test_the_control_disappears_once_a_discount_is_applied(self):
        txn = self.make_order(fulfillment_type='Pickup')
        self.apply(txn)

        body = self.client.get(
            reverse('transaction_detail', args=[txn.pk])
        ).content.decode()

        self.assertNotIn('Apply PWD / Senior Citizen discount', body)
        # ...and the trail is shown in its place.
        self.assertIn('Discount applied by', body)
