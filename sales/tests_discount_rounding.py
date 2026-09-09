"""A discounted line's stored total must survive SalesItem.save()'s recompute.

sales.pricing.price_line used to round the discounted line *total* last:

    net_unit = base - unit_discount            # full precision
    'unit_price': peso(net_unit),              # rounded to centavos
    'subtotal':   peso(net_unit * quantity),   # rounded from full precision

SalesItem.save() then independently recomputes ``subtotal = quantity_sold *
unit_price`` from the *frozen, already-rounded* unit_price. When quantity did
not divide the rounding evenly the two disagreed by a centavo, and
transaction_receipt's ``totals_disagree`` check lit up on a sale that was
arithmetically fine — ``line_sum`` (from the recomputed item subtotals) no
longer equalled the stored ``total_amount`` (from the round-last line total).

The fix rounds the per-unit net price once, and multiplies the rounded unit
by quantity — the same order SalesItem.save() uses — so the two agree by
construction:

    net_unit = peso(base - unit_discount)
    'unit_price': net_unit
    'subtotal':   net_unit * quantity

₱10.00 x 7 with the discount is the smallest reproducer: 10 / 1.12 = 8.9285…,
less 20% = 7.1428… -> ₱7.14 a unit. Round-last gave ₱50.00 for the line;
7 x ₱7.14 is ₱49.98. The stored total is now ₱49.98 and the recompute agrees.
"""
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product
from sales.models import SalesTransaction
from sales.pricing import price_cart, price_line


class _FakeProduct:
    """The shape price_line reads — same helper as core.tests_feature_update."""

    def __init__(self, price, vat_exempt=False, pwd=True, senior=True):
        self.unit_price = Decimal(price)
        self.is_vat_exempt = vat_exempt
        self.is_pwd_eligible = pwd
        self.is_senior_eligible = senior


class DiscountRoundingArithmeticTests(TestCase):
    """price_line / price_cart, no database. The centavo is asserted exactly."""

    def test_uneven_quantity_rounds_the_unit_then_multiplies(self):
        # ₱10.00 incl -> 8.9285… excl -> less 20% -> 7.1428… -> ₱7.14 a unit.
        line = price_line(_FakeProduct('10.00'), 7, discount_type='PWD')

        self.assertEqual(line['unit_price'], Decimal('7.14'))
        # qty x rounded unit, NOT peso(unrounded_unit x qty) which was 50.00.
        self.assertEqual(line['subtotal'], Decimal('49.98'))
        self.assertEqual(line['subtotal'], line['unit_price'] * 7)

    def test_uneven_quantity_line_reconciles_gross_discount_net(self):
        line = price_line(_FakeProduct('10.00'), 7, discount_type='Senior')

        # gross (pre-discount, VAT-exclusive) - discount == net subtotal.
        gross = (line['subtotal'] + line['discount_amount'])
        self.assertEqual(gross - line['discount_amount'], line['subtotal'])
        self.assertEqual(line['discount_amount'], Decimal('12.53'))

    def test_a_second_uneven_figure_has_the_same_shape(self):
        # ₱99.95 incl -> 89.2410… excl -> less 20% -> 71.3928… -> ₱71.39.
        line = price_line(_FakeProduct('99.95'), 4, discount_type='PWD')

        self.assertEqual(line['unit_price'], Decimal('71.39'))
        self.assertEqual(line['subtotal'], Decimal('285.56'))  # was 285.57
        self.assertEqual(line['subtotal'], line['unit_price'] * 4)

    def test_cart_total_equals_the_sum_of_recomputed_line_subtotals(self):
        # What SalesItem.save() + SalesTransaction do: qty x frozen unit,
        # summed. price_cart's total_amount must already equal that.
        cart = price_cart(
            [(_FakeProduct('10.00'), 7), (_FakeProduct('99.95'), 4)],
            discount_type='Senior',
        )
        recomputed = sum(
            (l['quantity'] * l['unit_price'] for l in cart['lines']),
            Decimal('0'),
        )
        self.assertEqual(cart['total_amount'], recomputed)
        self.assertEqual(cart['total_amount'], Decimal('335.54'))  # 49.98+285.56

    def test_evenly_dividing_cases_are_unchanged(self):
        # ₱112.00 incl -> ₱100.00 excl -> ₱80.00 a unit: divides cleanly, so
        # this branch behaved correctly before the fix and must still.
        by_three = price_line(_FakeProduct('112.00'), 3, discount_type='PWD')
        self.assertEqual(by_three['unit_price'], Decimal('80.00'))
        self.assertEqual(by_three['subtotal'], Decimal('240.00'))
        self.assertEqual(by_three['discount_amount'], Decimal('60.00'))

        by_one = price_line(_FakeProduct('10.00'), 1, discount_type='PWD')
        self.assertEqual(by_one['unit_price'], Decimal('7.14'))
        self.assertEqual(by_one['subtotal'], Decimal('7.14'))

    def test_the_non_discount_branches_are_untouched(self):
        # discount_type=None -> the storefront / DRF write path. Still rounds
        # the line total last; unchanged by this fix.
        vatable = price_line(_FakeProduct('10.00'), 7, discount_type=None)
        self.assertEqual(vatable['unit_price'], Decimal('10.00'))
        self.assertEqual(vatable['subtotal'], Decimal('70.00'))

        exempt = price_line(
            _FakeProduct('10.00', vat_exempt=True, pwd=False, senior=False),
            7, discount_type='PWD',
        )
        self.assertEqual(exempt['subtotal'], Decimal('70.00'))


class DiscountRoundingReceiptReconcilesTests(TestCase):
    """End to end: apply the discount through the Console, then render the
    receipt and confirm its totals-disagree guard stays quiet."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='dr-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.shopper = User.objects.create_user(
            username='dr-shopper', password='pw', role=User.Role.CUSTOMER
        )
        self.product = Product.objects.create(
            sku_code='SKU-DR', product_name='Loose item',
            unit_of_measure='pc', unit_price=Decimal('10.00'),
            is_vat_exempt=False, is_pwd_eligible=True, is_senior_eligible=True,
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )

    def make_pickup_order(self, quantity, unit_price='10.00'):
        """An undiscounted Online pickup order of `quantity` x `unit_price`,
        priced the way checkout writes one (VAT recorded inside the total)."""
        unit_price = Decimal(unit_price)
        gross = unit_price * quantity
        vat = (gross - gross / Decimal('1.12')).quantize(Decimal('0.01'))
        txn = SalesTransaction.objects.create(
            branch=self.branch,
            transaction_type='Online',
            fulfillment_type='Pickup',
            customer=self.shopper,
            delivery_address='',
            order_status=SalesTransaction.OrderStatus.PENDING,
            mode_of_payment='Cash on Delivery',
            total_amount=gross,
            total_vat_amount=vat,
        )
        txn.items.create(
            product=self.product, quantity_sold=quantity,
            unit_price=unit_price, vat_amount=vat, is_vat_exempt_line=False,
        )
        return txn

    def apply_discount(self, txn):
        self.client.force_login(self.owner)
        return self.client.post(
            reverse('transaction_apply_discount', args=[txn.pk]),
            {
                'discount_type': 'Senior',
                'discount_id_number': 'OSCA-12345',
                'discount_holder_name': 'Maria Santos',
            },
            follow=True,
        )

    def receipt_context(self, txn):
        response = self.client.get(
            reverse('transaction_receipt', args=[txn.pk])
        )
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_uneven_quantity_receipt_totals_agree(self):
        txn = self.make_pickup_order(quantity=7)

        self.apply_discount(txn)

        txn.refresh_from_db()
        item = txn.items.get()
        # price_line rounded the unit; SalesItem.save() recomputed the subtotal
        # from it and landed on the same figure.
        self.assertEqual(item.unit_price, Decimal('7.14'))
        self.assertEqual(item.subtotal, Decimal('49.98'))
        self.assertEqual(item.subtotal, item.unit_price * item.quantity_sold)
        self.assertEqual(txn.total_amount, Decimal('49.98'))

        ctx = self.receipt_context(txn)
        self.assertEqual(ctx['line_sum'], txn.total_amount)
        self.assertFalse(ctx['totals_disagree'])

    def test_a_second_uneven_figure_reconciles_too(self):
        txn = self.make_pickup_order(quantity=4, unit_price='99.95')

        self.apply_discount(txn)

        txn.refresh_from_db()
        item = txn.items.get()
        self.assertEqual(item.unit_price, Decimal('71.39'))
        self.assertEqual(item.subtotal, Decimal('285.56'))
        self.assertEqual(txn.total_amount, Decimal('285.56'))

        ctx = self.receipt_context(txn)
        self.assertFalse(ctx['totals_disagree'])

    def test_evenly_dividing_order_still_reconciles(self):
        txn = self.make_pickup_order(quantity=3, unit_price='112.00')

        self.apply_discount(txn)

        txn.refresh_from_db()
        item = txn.items.get()
        self.assertEqual(item.unit_price, Decimal('80.00'))
        self.assertEqual(item.subtotal, Decimal('240.00'))
        self.assertEqual(txn.total_amount, Decimal('240.00'))
        self.assertEqual(txn.total_discount_amount, Decimal('60.00'))

        ctx = self.receipt_context(txn)
        self.assertFalse(ctx['totals_disagree'])

    def test_undiscounted_uneven_order_is_billed_unchanged(self):
        """The non-discount path is not touched: an order that never gets a
        discount keeps exactly the total checkout wrote."""
        txn = self.make_pickup_order(quantity=7)
        before = txn.total_amount
        self.client.force_login(self.owner)

        ctx = self.receipt_context(txn)

        self.assertEqual(before, Decimal('70.00'))
        self.assertEqual(ctx['line_sum'], Decimal('70.00'))
        self.assertFalse(ctx['totals_disagree'])
