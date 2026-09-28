"""Tests for the owner-requested feature update.

Covers the five items in docs/feature-update-implementation-plan.md:
delivery scanning, supplier<->product mapping, withdrawal release scanning,
VAT / PWD-Senior pricing, and the printable purchase order.

Run with:
    python manage.py test core.tests_feature_update
"""
from decimal import Decimal

from django.core.management import call_command
from django.test import Client, TestCase

from accounts.models import User
from branches.models import Branch
from inventory.models import (
    Inventory, Product, StockTransfer, StockTransferItem, Warehouse, WarehouseStock,
)
from procurement.models import (
    DeliveryScanLog, Procurement, ProcurementItem, SupplierProduct,
)
from procurement.services import SupplierTransferError, transfer_supplier_products
from sales.models import SalesTransaction
from sales.pricing import price_cart, price_line


class PricingTests(TestCase):
    """The VAT / statutory-discount arithmetic, with no database involved.

    These are the numbers a customer disputes at the counter, so they are
    asserted exactly rather than approximately.
    """

    class FakeProduct:
        def __init__(self, price, vat_exempt=False, pwd=True, senior=True):
            self.unit_price = Decimal(price)
            self.is_vat_exempt = vat_exempt
            self.is_pwd_eligible = pwd
            self.is_senior_eligible = senior

    def test_discount_comes_off_the_vat_exclusive_price(self):
        # ₱112.00 inclusive -> ₱100.00 exclusive -> less 20% -> ₱80.00.
        # Taking 20% off ₱112.00 would charge ₱89.60, which is the mistake
        # this ordering exists to prevent.
        line = price_line(self.FakeProduct('112.00'), 1, discount_type='PWD')
        self.assertEqual(line['unit_price'], Decimal('80.00'))
        self.assertEqual(line['discount_amount'], Decimal('20.00'))
        self.assertNotEqual(line['unit_price'], Decimal('89.60'))

    def test_a_discounted_line_becomes_vat_exempt(self):
        line = price_line(self.FakeProduct('112.00'), 1, discount_type='Senior')
        self.assertTrue(line['is_vat_exempt_line'])
        self.assertEqual(line['vat_amount'], Decimal('0.00'))

    def test_an_ineligible_product_is_untouched_by_a_claim(self):
        line = price_line(
            self.FakeProduct('112.00', pwd=False, senior=False), 1,
            discount_type='PWD',
        )
        self.assertEqual(line['unit_price'], Decimal('112.00'))
        self.assertEqual(line['discount_amount'], Decimal('0.00'))

    def test_each_claim_type_reads_its_own_eligibility_flag(self):
        # The whole point of splitting the flag: a product can be covered by
        # one statute and excluded from the other.
        senior_only = self.FakeProduct('112.00', pwd=False, senior=True)

        under_senior = price_line(senior_only, 1, discount_type='Senior')
        under_pwd = price_line(senior_only, 1, discount_type='PWD')

        self.assertEqual(under_senior['unit_price'], Decimal('80.00'))
        self.assertEqual(under_pwd['unit_price'], Decimal('112.00'))

    def test_no_claim_type_means_no_discount(self):
        # A product ticked for both still pays full price when nobody
        # presented a card.
        for claim in (None, 'None', ''):
            line = price_line(self.FakeProduct('112.00'), 1, discount_type=claim)
            self.assertEqual(line['unit_price'], Decimal('112.00'))
            self.assertEqual(line['discount_amount'], Decimal('0.00'))

    def test_an_exempt_product_is_not_vat_stripped_before_discounting(self):
        # Its shelf price never contained VAT, so the 20% comes off the whole
        # ₱100.00, not off ₱89.29.
        line = price_line(
            self.FakeProduct('100.00', vat_exempt=True), 1, discount_type='PWD'
        )
        self.assertEqual(line['unit_price'], Decimal('80.00'))

    def test_an_undiscounted_vatable_line_still_charges_the_shelf_price(self):
        # VAT is recorded for reporting but never added on top — the customer
        # pays what the shelf says.
        line = price_line(self.FakeProduct('112.00'), 2, discount_type=None)
        self.assertEqual(line['subtotal'], Decimal('224.00'))
        self.assertEqual(line['vat_amount'], Decimal('24.00'))

    def test_cart_totals_reconcile(self):
        cart = price_cart(
            [
                (self.FakeProduct('112.00'), 2),
                (self.FakeProduct('50.00', pwd=False, senior=False), 1),
                (self.FakeProduct('100.00', vat_exempt=True), 3),
            ],
            discount_type='Senior',
        )
        self.assertEqual(
            cart['gross_amount'] - cart['total_discount_amount'],
            cart['total_amount'],
        )
        self.assertEqual(cart['total_amount'], Decimal('450.00'))
        self.assertEqual(cart['total_discount_amount'], Decimal('100.00'))


class FeatureUpdateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def setUp(self):
        # Mapping tests below construct their own exact source/destination
        # catalogs. The demo seed now also creates realistic catalogs for the
        # PO picker, so isolate these transfer assertions from that fixture.
        SupplierProduct.objects.all().delete()

    def login(self, username):
        client = Client()
        client.force_login(User.objects.get(username=username))
        return client

    def owner(self):
        return User.objects.filter(role=User.Role.OWNER).first()

    def open_po(self):
        """An Incoming PO with items, ready to receive."""
        return (
            Procurement.objects
            .filter(status=Procurement.Status.INCOMING)
            .filter(items__isnull=False)
            .distinct()
            .first()
        )

    # ------------------------------------------------------------- PO print
    def test_printable_po_carries_every_required_field(self):
        proc = Procurement.objects.filter(items__isnull=False).first()
        response = self.login(self.owner().username).get(
            f'/procurement/{proc.pk}/print/'
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        for expected in [
            proc.po_number,                     # PO number
            'PURCHASE ORDER',
            'Target Delivery Date',
            'Prepared By',
            'Authorized By',                    # signature blocks
            proc.branch.branch_name,            # deliver-to
        ]:
            self.assertIn(expected, body)

        for item in proc.items.all():
            self.assertIn(item.product.product_name, body)

    def test_po_line_totals_are_not_zero(self):
        # procurement_detail.html referenced item.line_total before the
        # property existed, so every line printed ₱0.00.
        item = ProcurementItem.objects.filter(quantity_ordered__gt=0).first()
        self.assertEqual(
            item.line_total, item.quantity_ordered * item.cost_price
        )
        self.assertGreater(item.line_total, 0)

    def test_printing_a_po_with_no_recorded_people_still_renders(self):
        # created_by postdates the older purchase orders and owner is only set
        # when an Owner raised the PO, so both are routinely null. Chaining
        # `{{ x.get_full_name|default:x.username }}` through a null FK raises
        # VariableDoesNotExist — a filter argument is resolved strictly, so
        # this 500'd on real data even though every seeded PO had them set.
        proc = Procurement.objects.filter(items__isnull=False).first()
        proc.created_by = None
        proc.owner = None
        proc.save()

        response = self.login(self.owner().username).get(
            f'/procurement/{proc.pk}/print/'
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('Prepared By', response.content.decode())

    def test_printing_a_po_with_no_supplier_still_renders(self):
        proc = Procurement.objects.filter(items__isnull=False).first()
        proc.supplier = None
        proc.save()

        response = self.login(self.owner().username).get(
            f'/procurement/{proc.pk}/print/'
        )
        self.assertEqual(response.status_code, 200)

    def test_a_supplier_cannot_print_another_suppliers_po(self):
        proc = Procurement.objects.exclude(supplier=None).first()
        intruder = User.objects.filter(
            role=User.Role.SUPPLIER
        ).exclude(pk=proc.supplier_id).first()
        self.assertIsNotNone(intruder, 'need a second supplier for this test')

        response = self.login(intruder.username).get(
            f'/procurement/{proc.pk}/print/'
        )
        self.assertRedirects(response, '/procurement/')

    # ------------------------------------------- supplier <-> product mapping
    def test_transfer_moves_products_between_suppliers(self):
        source, dest = list(User.objects.filter(role=User.Role.SUPPLIER)[:2])
        products = list(Product.objects.all()[:3])
        for product in products:
            SupplierProduct.objects.create(supplier=source, product=product)

        moved, merged = transfer_supplier_products(
            [p.pk for p in products], source, dest
        )

        self.assertEqual((moved, merged), (3, 0))
        self.assertEqual(SupplierProduct.objects.filter(supplier=source).count(), 0)
        self.assertEqual(SupplierProduct.objects.filter(supplier=dest).count(), 3)

    def test_transfer_consolidates_instead_of_hitting_the_unique_constraint(self):
        source, dest = list(User.objects.filter(role=User.Role.SUPPLIER)[:2])
        shared = Product.objects.first()
        SupplierProduct.objects.create(supplier=source, product=shared)
        SupplierProduct.objects.create(
            supplier=dest, product=shared, default_cost_price=Decimal('99.00')
        )

        moved, merged = transfer_supplier_products([shared.pk], source, dest)

        self.assertEqual((moved, merged), (0, 1))
        # The destination's own price survives — it is what that supplier
        # actually quotes.
        self.assertEqual(
            SupplierProduct.objects.get(supplier=dest, product=shared).default_cost_price,
            Decimal('99.00'),
        )

    def test_transfer_does_not_rewrite_purchase_order_history(self):
        proc = Procurement.objects.exclude(supplier=None).filter(
            items__isnull=False
        ).first()
        original_supplier = proc.supplier
        product = proc.items.first().product
        other = User.objects.filter(
            role=User.Role.SUPPLIER
        ).exclude(pk=original_supplier.pk).first()

        SupplierProduct.objects.create(supplier=original_supplier, product=product)
        transfer_supplier_products([product.pk], original_supplier, other)

        proc.refresh_from_db()
        self.assertEqual(proc.supplier, original_supplier)

    def test_transfer_to_the_same_supplier_is_refused(self):
        supplier = User.objects.filter(role=User.Role.SUPPLIER).first()
        product = Product.objects.first()
        SupplierProduct.objects.create(supplier=supplier, product=product)
        with self.assertRaises(SupplierTransferError):
            transfer_supplier_products([product.pk], supplier, supplier)

    def test_supplier_detail_lists_that_suppliers_products(self):
        supplier = User.objects.filter(role=User.Role.SUPPLIER).first()
        product = Product.objects.first()
        SupplierProduct.objects.create(supplier=supplier, product=product)

        response = self.login(self.owner().username).get(
            f'/procurement/suppliers/{supplier.pk}/'
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(product.product_name, response.content.decode())

    # ------------------------------------------------------ delivery scanning
    def test_scanning_a_po_item_reports_a_match_and_logs_it(self):
        proc = self.open_po()
        item = proc.items.first()
        item.product.barcode = 'SCANTEST-MATCH'
        item.product.save()

        response = self.login(self.owner().username).post(
            f'/procurement/deliveries/{proc.pk}/scan/', {'code': 'SCANTEST-MATCH'}
        )
        payload = response.json()

        self.assertEqual(payload['status'], 'matched')
        self.assertEqual(payload['item_id'], item.pk)
        self.assertTrue(
            DeliveryScanLog.objects.filter(
                procurement=proc, procurement_item=item, was_unexpected=False
            ).exists()
        )

    def test_scanning_a_product_not_on_the_order_is_flagged_not_added(self):
        proc = self.open_po()
        stranger = Product.objects.exclude(
            pk__in=proc.items.values_list('product_id', flat=True)
        ).first()
        stranger.barcode = 'SCANTEST-STRANGER'
        stranger.save()
        before = proc.items.count()

        payload = self.login(self.owner().username).post(
            f'/procurement/deliveries/{proc.pk}/scan/', {'code': 'SCANTEST-STRANGER'}
        ).json()

        self.assertEqual(payload['status'], 'not_on_order')
        # Scanning never mutates the order — adding is a separate, deliberate act.
        self.assertEqual(proc.items.count(), before)
        self.assertTrue(
            DeliveryScanLog.objects.filter(
                procurement=proc, was_unexpected=True,
                matched_product=stranger,
            ).exists()
        )

    def test_an_unknown_code_is_logged_with_no_product(self):
        proc = self.open_po()
        payload = self.login(self.owner().username).post(
            f'/procurement/deliveries/{proc.pk}/scan/', {'code': 'NO-SUCH-CODE'}
        ).json()

        self.assertEqual(payload['status'], 'unknown')
        self.assertTrue(
            DeliveryScanLog.objects.filter(
                procurement=proc, scanned_code='NO-SUCH-CODE',
                matched_product=None, was_unexpected=True,
            ).exists()
        )

    def test_scanning_does_not_change_received_quantities(self):
        # The count lives in the form until submit, so a mis-scan is fixed by
        # editing the field rather than by reversing a stock movement.
        proc = self.open_po()
        item = proc.items.first()
        item.product.barcode = 'SCANTEST-NOMUTATE'
        item.product.save()
        before = item.quantity_received

        self.login(self.owner().username).post(
            f'/procurement/deliveries/{proc.pk}/scan/', {'code': 'SCANTEST-NOMUTATE'}
        )

        item.refresh_from_db()
        self.assertEqual(item.quantity_received, before)

    def test_an_unexpected_item_can_be_received_and_flags_a_discrepancy(self):
        proc = self.open_po()
        client = self.login(self.owner().username)
        stranger = Product.objects.exclude(
            pk__in=proc.items.values_list('product_id', flat=True)
        ).first()

        warehouse = Warehouse.objects.get(branch=proc.branch)
        stock_before = WarehouseStock.objects.filter(
            warehouse=warehouse, product=stranger
        ).values_list('quantity_on_hand', flat=True).first() or 0

        client.post(
            f'/procurement/deliveries/{proc.pk}/add-unexpected/',
            {'product': stranger.pk},
        )
        added = proc.items.get(product=stranger)
        self.assertTrue(added.is_unexpected)
        self.assertEqual(added.quantity_ordered, 0)

        items = list(proc.items.all())
        client.post(f'/procurement/deliveries/{proc.pk}/receive/', {
            'item_id': [str(i.pk) for i in items],
            'quantity_received': [
                str(5 if i.is_unexpected else i.quantity_ordered) for i in items
            ],
        })

        proc.refresh_from_db()
        added.refresh_from_db()
        self.assertEqual(proc.status, Procurement.Status.RECEIVED)
        # Goods nobody ordered always need a human to look at them.
        self.assertEqual(proc.receipt_flag, Procurement.ReceiptFlag.DISCREPANCY)
        self.assertEqual(added.quantity_received, 5)

        # Goods that arrived unordered still reach the warehouse — the point
        # of recording them is that the digital count matches the physical one.
        self.assertEqual(
            WarehouseStock.objects.get(
                warehouse=warehouse, product=stranger
            ).quantity_on_hand,
            stock_before + 5,
        )

    def test_over_delivery_on_an_ordered_line_is_still_refused(self):
        # A deliberate pre-existing control: more than was ordered needs a
        # human, and the unexpected-item path is the sanctioned way to record it.
        proc = self.open_po()
        item = proc.items.first()

        self.login(self.owner().username).post(
            f'/procurement/deliveries/{proc.pk}/receive/', {
                'item_id': [str(item.pk)],
                'quantity_received': [str(item.quantity_ordered + 10)],
            }
        )

        proc.refresh_from_db()
        self.assertEqual(proc.status, Procurement.Status.INCOMING)

    # -------------------------------------------------- withdrawal release
    def approved_slip(self, quantity=10):
        owner = self.owner()
        branch = Branch.objects.first()
        warehouse = Warehouse.objects.get(branch=branch)
        product = Product.objects.first()
        stock, _ = WarehouseStock.objects.get_or_create(
            warehouse=warehouse, product=product,
            defaults={'quantity_on_hand': 0},
        )
        stock.quantity_on_hand = 100
        stock.save()

        slip = StockTransfer.objects.create(
            warehouse=warehouse, source_branch=branch, destination_branch=branch,
            purpose=StockTransfer.Purpose.BRANCH_RESTOCK,
            requested_by=owner, approved_by=owner,
            status=StockTransfer.Status.APPROVED,
        )
        item = StockTransferItem.objects.create(
            transfer=slip, product=product,
            quantity_requested=quantity, quantity_approved=quantity,
        )
        return slip, item, stock

    def test_release_moves_only_what_was_actually_picked(self):
        slip, item, stock = self.approved_slip(quantity=10)

        self.login(self.owner().username).post(
            f'/inventory/withdrawals/{slip.pk}/release/', {
                'item_id': [str(item.pk)],
                'quantity_released': ['7'],
                'release_method': ['Scan'],
            }
        )

        slip.refresh_from_db()
        item.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(slip.status, StockTransfer.Status.RELEASED)
        self.assertEqual(item.quantity_released, 7)
        self.assertEqual(item.release_method, StockTransferItem.ReleaseMethod.SCAN)
        self.assertEqual(stock.quantity_on_hand, 93)   # 7 moved, not 10

    def test_picking_more_than_approved_is_refused(self):
        slip, item, stock = self.approved_slip(quantity=5)

        self.login(self.owner().username).post(
            f'/inventory/withdrawals/{slip.pk}/release/', {
                'item_id': [str(item.pk)],
                'quantity_released': ['9'],
                'release_method': ['Scan'],
            }
        )

        slip.refresh_from_db()
        stock.refresh_from_db()
        # The approver set the ceiling; a picker cannot raise it by scanning.
        self.assertEqual(slip.status, StockTransfer.Status.APPROVED)
        self.assertEqual(stock.quantity_on_hand, 100)

    def test_a_post_without_picked_quantities_releases_what_was_approved(self):
        # Backwards compatibility: the old detail-page button posted no
        # per-item fields, and must still mean "release exactly what was
        # approved" rather than "release nothing".
        slip, item, stock = self.approved_slip(quantity=4)

        self.login(self.owner().username).post(
            f'/inventory/withdrawals/{slip.pk}/release/', {}
        )

        slip.refresh_from_db()
        item.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(slip.status, StockTransfer.Status.RELEASED)
        self.assertEqual(item.quantity_released, 4)
        self.assertEqual(stock.quantity_on_hand, 96)

    def test_scanning_a_product_not_on_the_slip_is_refused(self):
        slip, item, _ = self.approved_slip()
        stranger = Product.objects.exclude(pk=item.product_id).first()
        stranger.barcode = 'SLIPTEST-STRANGER'
        stranger.save()

        payload = self.login(self.owner().username).post(
            f'/inventory/withdrawals/{slip.pk}/scan/', {'code': 'SLIPTEST-STRANGER'}
        ).json()

        self.assertEqual(payload['status'], 'not_on_slip')

    # ------------------------------------------------------ checkout pricing
    def sell(self, client, branch, product, quantity, **extra):
        payload = {
            'branch': branch.pk,
            'product_id': [str(product.pk)],
            'quantity': [str(quantity)],
            'mode_of_payment': 'Cash',
            # Cash sales require it (sales.views.transaction_add) — a flat,
            # generously large amount so it always covers whatever this
            # call's real total comes out to, since these tests care about
            # pricing/discount arithmetic, not the cash-tendered flow itself.
            'amount_tendered': '100000',
        }
        payload.update(extra)
        return client.post('/sales/add/', payload, follow=True)

    def vatable_product(self, price='112.00'):
        product = Product.objects.first()
        product.unit_price = Decimal(price)
        product.is_vat_exempt = False
        product.is_pwd_eligible = True
        product.is_senior_eligible = True
        product.save()
        branch = Branch.objects.first()
        inventory, _ = Inventory.objects.get_or_create(
            product=product, branch=branch
        )
        inventory.quantity_on_hand = 500
        # seed_demo populates some already-expired rows to exercise the
        # Alerts Center's Expired tab — get_or_create above can land on one
        # of those, and an expired row is no longer sellable (see
        # Inventory.objects.sellable()). This helper is testing pricing, not
        # expiry, so it always clears the field explicitly.
        inventory.expiry_date = None
        inventory.save()
        return product, branch

    def test_a_senior_discount_is_applied_and_recorded(self):
        product, branch = self.vatable_product()

        self.sell(
            self.login(self.owner().username), branch, product, 2,
            discount_type='Senior',
            discount_id_number='OSCA-12345',
            discount_holder_name='Maria Santos',
        )

        txn = SalesTransaction.objects.latest('pk')
        self.assertEqual(txn.total_amount, Decimal('160.00'))       # 2 x ₱80.00
        self.assertEqual(txn.total_discount_amount, Decimal('40.00'))
        self.assertEqual(txn.total_vat_amount, Decimal('0.00'))
        self.assertEqual(txn.discount_id_number, 'OSCA-12345')
        self.assertEqual(txn.billing_record.amount_due, Decimal('160.00'))

        item = txn.items.first()
        self.assertEqual(item.unit_price, Decimal('80.00'))
        self.assertTrue(item.is_vat_exempt_line)

    def test_a_claim_without_an_id_number_is_refused(self):
        product, branch = self.vatable_product()
        before = SalesTransaction.objects.count()

        response = self.sell(
            self.login(self.owner().username), branch, product, 2,
            discount_type='Senior', discount_holder_name='Maria Santos',
        )

        self.assertIn('ID number', response.content.decode())
        self.assertEqual(SalesTransaction.objects.count(), before)

    def test_an_undiscounted_sale_bills_exactly_as_before(self):
        product, branch = self.vatable_product()

        self.sell(self.login(self.owner().username), branch, product, 2)

        txn = SalesTransaction.objects.latest('pk')
        self.assertEqual(txn.total_amount, Decimal('224.00'))
        self.assertEqual(txn.discount_type, SalesTransaction.DiscountType.NONE)
        self.assertEqual(txn.total_discount_amount, Decimal('0.00'))
        # VAT is newly recorded for reporting, but was never added to the bill.
        self.assertEqual(txn.total_vat_amount, Decimal('24.00'))

    def test_an_ineligible_product_stays_at_full_price_under_a_claim(self):
        product, branch = self.vatable_product()
        product.is_pwd_eligible = False
        product.is_senior_eligible = False
        product.save()

        self.sell(
            self.login(self.owner().username), branch, product, 1,
            discount_type='PWD',
            discount_id_number='PWD-999',
            discount_holder_name='Jose Cruz',
        )

        txn = SalesTransaction.objects.latest('pk')
        self.assertEqual(txn.total_amount, Decimal('112.00'))
        self.assertEqual(txn.total_discount_amount, Decimal('0.00'))

    def test_a_senior_only_product_is_not_discounted_under_a_pwd_claim(self):
        # End-to-end version of the split: the two statutes are gated
        # separately all the way through to what the customer is billed.
        product, branch = self.vatable_product()
        product.is_pwd_eligible = False
        product.save()

        self.sell(
            self.login(self.owner().username), branch, product, 1,
            discount_type='PWD',
            discount_id_number='PWD-999',
            discount_holder_name='Jose Cruz',
        )
        self.assertEqual(
            SalesTransaction.objects.latest('pk').total_amount, Decimal('112.00')
        )

        self.sell(
            self.login(self.owner().username), branch, product, 1,
            discount_type='Senior',
            discount_id_number='OSCA-12345',
            discount_holder_name='Maria Santos',
        )
        self.assertEqual(
            SalesTransaction.objects.latest('pk').total_amount, Decimal('80.00')
        )

    def test_a_new_product_is_eligible_for_neither_discount_by_default(self):
        # Nothing is ticked for the user: eligibility has to be chosen, so a
        # product added without touching the boxes never quietly discounts.
        self.login(self.owner().username).post('/inventory/products/add/', {
            'product_name': 'Untouched Boxes Product',
            'unit_of_measure': Product.UnitOfMeasure.PIECE,
            'unit_price': '112.00',
        }, follow=True)

        product = Product.objects.get(product_name='Untouched Boxes Product')
        self.assertFalse(product.is_pwd_eligible)
        self.assertFalse(product.is_senior_eligible)
        self.assertFalse(product.is_vat_exempt)
