"""P0 — POST /api/sales/ had two independent ways to disagree with itself.

1. transactionType was taken on the client's word. A Customer could post
   transactionType=Physical and land on the branch that deducts stock at
   creation time — the one meant only for the counter/console, where a human
   has actually handed goods over. SalesTransactionWriteSerializer.validate()
   now derives the channel server-side: any Customer-created sale is Online,
   whatever the payload claims.

2. A cart that split one product across two lines ran the availability
   check and the stock deduction once per LINE, each against its own
   independently select_for_update()'d Inventory instance. Two lines of 5
   could each pass an 8-in-stock check that their 10-unit total should have
   failed, and the second line's .save() clobbered the first line's
   decrement instead of building on it — recorded sales and actual stock
   disagreed. create() now locks each product's Inventory row once and
   shares that instance across every line for that product.
"""
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from branches.models import Branch
from inventory.models import Inventory, Product, StockMovement
from sales.models import SalesTransaction


class SalesApiCreationSecurityTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.product = Product.objects.create(
            sku_code='SKU-001', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('100.00'),
        )
        self.product.price_tiers.create(
            min_quantity=6, unit_price=Decimal('80.00')
        )
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=8, reorder_level=1,
        )

    def customer(self, username='shopper'):
        return User.objects.create_user(
            username=username, password='pw', role=User.Role.CUSTOMER
        )

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


class CustomerChannelIsServerDerivedTests(SalesApiCreationSecurityTestCase):

    def test_a_customer_posting_physical_is_recorded_as_online(self):
        response = self.post_sale(
            self.customer(),
            transactionType=SalesTransaction.TransactionType.PHYSICAL,
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.transaction_type, SalesTransaction.TransactionType.ONLINE)

    def test_a_customer_posting_physical_does_not_deduct_stock_at_creation(self):
        before = self.inventory.quantity_on_hand

        response = self.post_sale(
            self.customer(),
            transactionType=SalesTransaction.TransactionType.PHYSICAL,
        )

        self.assertEqual(response.status_code, 201)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, before)
        self.assertFalse(StockMovement.objects.exists())

    def test_a_customer_claiming_physical_still_needs_a_delivery_address(self):
        """Physical never needed a delivery address; forcing the channel to
        Online for a Customer must not let them skip the requirement that
        comes bundled with it."""
        response = self.post_sale(
            self.customer(),
            transactionType=SalesTransaction.TransactionType.PHYSICAL,
            deliveryAddress='',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('deliveryAddress', response.data)
        self.assertFalse(SalesTransaction.objects.exists())

    def test_an_owner_recorded_physical_sale_is_unaffected(self):
        """The override is scoped to Customer — a genuine counter sale
        recorded through this endpoint by internal staff must still work."""
        response = self.post_sale(
            self.owner,
            transactionType=SalesTransaction.TransactionType.PHYSICAL,
            deliveryAddress='',
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.transaction_type, SalesTransaction.TransactionType.PHYSICAL)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 6)  # 8 - 2 ordered


class DuplicateProductLineAggregationTests(SalesApiCreationSecurityTestCase):

    def test_duplicate_lines_whose_total_exceeds_stock_are_rejected(self):
        """8 on hand; two lines of 5 each (10 total) must fail as a whole,
        not pass because each individual line looked fine against the
        untouched stock figure."""
        response = self.post_sale(
            self.customer(),
            items=[
                {'productId': self.product.pk, 'quantity': 5},
                {'productId': self.product.pk, 'quantity': 5},
            ],
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SalesTransaction.objects.exists())
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 8, 'a rejected sale must not touch stock')

    def test_duplicate_lines_within_stock_are_primarily_priced_at_the_bulk_tier(self):
        """3 + 3 = 6, which reaches this fixture's 6-unit tier. Each line
        must earn the break, the same as one combined line of 6 would."""
        response = self.post_sale(
            self.customer(),
            items=[
                {'productId': self.product.pk, 'quantity': 3},
                {'productId': self.product.pk, 'quantity': 3},
            ],
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(
            sorted(item.unit_price for item in sale.items.all()),
            [Decimal('80.00'), Decimal('80.00')],
        )
        self.assertEqual(sale.total_amount, Decimal('480.00'))  # 6 x 80.00

    def test_duplicate_physical_lines_deduct_the_true_combined_total_exactly_once(self):
        """Same 3 + 3 cart, but Physical so it deducts at creation. The bug
        deducted only the LAST line against a stale re-fetched Inventory
        instance, clobbering the first line's decrement; the fix must
        deduct the full 6, once."""
        response = self.post_sale(
            self.owner,
            transactionType=SalesTransaction.TransactionType.PHYSICAL,
            deliveryAddress='',
            items=[
                {'productId': self.product.pk, 'quantity': 3},
                {'productId': self.product.pk, 'quantity': 3},
            ],
        )

        self.assertEqual(response.status_code, 201)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 2)  # 8 - 6, not 8 - 3

        movements = StockMovement.objects.order_by('pk')
        self.assertEqual(movements.count(), 2)
        self.assertEqual(sum(m.quantity_change for m in movements), -6)
        # Each movement's own before/after must chain onto the previous
        # one's result, not repeat the original pre-sale figure.
        self.assertEqual(movements[0].quantity_before, 8)
        self.assertEqual(movements[0].quantity_after, 5)
        self.assertEqual(movements[1].quantity_before, 5)
        self.assertEqual(movements[1].quantity_after, 2)

    def test_three_lines_of_the_same_product_aggregate_correctly(self):
        """Not just the two-line case — an arbitrary split must still sum
        to the right total demand and the right total deduction."""
        response = self.post_sale(
            self.owner,
            transactionType=SalesTransaction.TransactionType.PHYSICAL,
            deliveryAddress='',
            items=[
                {'productId': self.product.pk, 'quantity': 2},
                {'productId': self.product.pk, 'quantity': 2},
                {'productId': self.product.pk, 'quantity': 2},
            ],
        )

        self.assertEqual(response.status_code, 201)
        sale = SalesTransaction.objects.get()
        self.assertEqual(sale.items.count(), 3)
        # 6 total reaches the tier, same as the two-line case.
        self.assertTrue(all(item.unit_price == Decimal('80.00') for item in sale.items.all()))
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity_on_hand, 2)  # 8 - 6
