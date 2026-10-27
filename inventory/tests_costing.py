"""What a unit of stock cost (inventory/costing.py): where the number comes from,
that it is frozen onto the sale line, and that it follows received deliveries.
"""
import datetime as dt
import importlib
from decimal import Decimal
from io import StringIO

from django.apps import apps as django_apps
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from inventory import costing
from inventory.models import Batch, Inventory, Product, ProductCategory, StockMovement
from procurement.models import Procurement, ProcurementItem, SupplierProduct
from procurement.services import record_delivery
from sales.models import SalesItem, SalesItemBatchConsumption, SalesTransaction
from sales.views import _deduct_stock

D = Decimal
FLOOR, WAREHOUSE = Batch.Location.FLOOR, Batch.Location.WAREHOUSE


class CostFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.owner = User.objects.create_user(username='cost-owner', password='pw', role=User.Role.OWNER)
        cls.supplier = User.objects.create_user(username='cost-sup', password='pw', role=User.Role.SUPPLIER)
        cls.branch = Branch.objects.create(branch_name='Cost Branch', location='Baliuag', owner=cls.owner)
        cls.product = Product.objects.create(
            category=ProductCategory.objects.create(category_name='Cost goods'),
            sku_code='CO-1', product_name='Canned Tuna', unit_price=D('50'))

    def po(self, unit_cost, product=None, status=Procurement.Status.ORDERED, received=20, ordered=20):
        po = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner, order_date=self.today,
            status=status, total_cost=D('0'))
        ProcurementItem.objects.create(
            procurement=po, product=product or self.product, quantity_ordered=ordered,
            quantity_received=received, unit_cost_snapshot=None if unit_cost is None else D(unit_cost))
        return po

    def batch(self, qty, *, location=FLOOR, procurement=None, source=None, expiry=None, product=None):
        return Batch.objects.create(
            product=product or self.product, branch=self.branch, location=location, quantity=qty,
            procurement=procurement, source_batch=source, received_date=self.today, expiry_date=expiry)

    def floor_stock(self, qty):
        Inventory.objects.update_or_create(
            product=self.product, branch=self.branch, defaults={'quantity_on_hand': qty, 'reorder_level': 0})

    def sale(self, qty, status=SalesTransaction.OrderStatus.COMPLETED, product=None):
        txn = SalesTransaction.objects.create(
            branch=self.branch, total_amount=D(qty) * D('50'), recorded_by=self.owner,
            transaction_type='Physical', mode_of_payment='Cash')
        SalesTransaction.objects.filter(pk=txn.pk).update(order_status=status)
        item = SalesItem.objects.create(
            transaction=txn, product=product or self.product, quantity_sold=qty, unit_price=D('50'))
        return txn, item


class ResolverTests(CostFixture):
    def test_a_batch_is_costed_from_its_purchase_order_line(self):
        batch = self.batch(20, procurement=self.po('12.00'))
        self.assertEqual(costing.CostResolver().batch_cost(batch), D('12.00'))

    def test_a_floor_batch_copied_from_a_warehouse_batch_keeps_its_cost(self):
        warehouse = self.batch(20, location=WAREHOUSE, procurement=self.po('12.00'))
        # Made by a restock: procurement copied across (inventory.views).
        copied = self.batch(10, procurement=warehouse.procurement, source=warehouse)
        # ...and one that only kept the link back to its source.
        linked_only = self.batch(10, source=warehouse)
        resolver = costing.CostResolver()
        self.assertEqual(resolver.batch_cost(copied), D('12.00'))
        self.assertEqual(resolver.batch_cost(linked_only), D('12.00'))

    def test_a_batch_with_no_trail_has_no_cost(self):
        self.assertIsNone(costing.CostResolver().batch_cost(self.batch(5)))

    def test_a_purchase_order_line_without_a_price_does_not_cost_the_batch(self):
        self.assertIsNone(costing.CostResolver().batch_cost(self.batch(5, procurement=self.po(None))))

    def test_a_cycle_in_the_source_chain_ends_instead_of_looping(self):
        a = self.batch(1)
        b = self.batch(1, source=a)
        Batch.objects.filter(pk=a.pk).update(source_batch=b)
        a.refresh_from_db()
        self.assertIsNone(costing.CostResolver().batch_cost(a))

    def test_two_lots_give_a_quantity_weighted_average(self):
        cheap = self.batch(10, procurement=self.po('12.00'))
        dear = self.batch(5, procurement=self.po('18.00'))
        cost = costing.sale_unit_cost(self.product, [(cheap, 10), (dear, 5)], 15)
        self.assertEqual(cost, D('14.0000'))                  # (10 x 12 + 5 x 18) / 15

    def test_an_average_that_is_not_a_whole_centavo_keeps_four_places(self):
        a = self.batch(1, procurement=self.po('10.00'))
        b = self.batch(2, procurement=self.po('10.01'))
        self.assertEqual(costing.sale_unit_cost(self.product, [(a, 1), (b, 2)], 3), D('10.0067'))

    def test_a_batch_with_no_cost_falls_back_to_the_products_latest_cost(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('9.00'))
        self.product.refresh_from_db()
        untraced = self.batch(5)
        self.assertEqual(costing.sale_unit_cost(self.product, [(untraced, 5)], 5), D('9.0000'))

    def test_a_mix_of_traced_and_untraced_lots_is_averaged(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('10.00'))
        self.product.refresh_from_db()
        traced = self.batch(5, procurement=self.po('20.00'))
        untraced = self.batch(5)
        self.assertEqual(costing.sale_unit_cost(self.product, [(traced, 5), (untraced, 5)], 10), D('15.0000'))

    def test_nothing_known_means_unknown_not_zero(self):
        self.assertIsNone(costing.sale_unit_cost(self.product, [(self.batch(5), 5)], 5))
        self.assertIsNone(costing.sale_unit_cost(self.product, [], 5))

    def test_one_unpriceable_lot_makes_the_whole_line_unknown(self):
        traced = self.batch(5, procurement=self.po('20.00'))
        untraced = self.batch(5)
        self.assertIsNone(costing.sale_unit_cost(self.product, [(traced, 5), (untraced, 5)], 10))

    def test_units_the_batches_did_not_cover_are_priced_at_the_latest_cost(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('30.00'))
        self.product.refresh_from_db()
        traced = self.batch(2, procurement=self.po('10.00'))
        self.assertEqual(costing.sale_unit_cost(self.product, [(traced, 2)], 4), D('20.0000'))

    def test_a_deleted_product_with_traced_batches_is_still_costed(self):
        traced = self.batch(2, procurement=self.po('10.00'))
        self.assertEqual(costing.sale_unit_cost(None, [(traced, 2)], 2), D('10.0000'))


class FrozenAtSaleTests(CostFixture):
    def test_deducting_stock_freezes_the_cost_of_the_lots_it_used(self):
        po_old, po_new = self.po('12.00'), self.po('18.00')
        self.batch(10, procurement=po_old, expiry=self.today + dt.timedelta(days=30))     # sold first (FEFO)
        self.batch(10, procurement=po_new, expiry=self.today + dt.timedelta(days=90))
        self.floor_stock(20)
        txn, item = self.sale(15)
        self.assertIsNone(item.unit_cost)                                  # nothing is stamped before stock moves
        _deduct_stock(txn, self.owner)
        item.refresh_from_db()
        self.assertEqual(item.unit_cost, D('14.0000'))                     # 10 x 12 then 5 x 18
        self.assertEqual(SalesItemBatchConsumption.objects.filter(sales_item=item).count(), 2)

    def test_a_later_price_change_never_touches_a_frozen_line(self):
        po = self.po('12.00')
        self.batch(10, procurement=po)
        self.floor_stock(10)
        txn, item = self.sale(4)
        _deduct_stock(txn, self.owner)
        ProcurementItem.objects.filter(procurement=po).update(unit_cost_snapshot=D('99.00'))
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('77.00'))
        item.refresh_from_db()
        self.assertEqual(item.unit_cost, D('12.0000'))

    def test_a_sale_is_costed_at_the_product_cost_when_the_lot_has_no_purchase_order(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('9.00'))
        self.batch(10)                                                     # opening stock
        self.floor_stock(10)
        txn, item = self.sale(2)
        _deduct_stock(txn, self.owner)
        item.refresh_from_db()
        self.assertEqual(item.unit_cost, D('9.0000'))

    def test_an_untraceable_sale_with_no_product_cost_is_left_unknown(self):
        self.batch(10)
        self.floor_stock(10)
        txn, item = self.sale(2)
        _deduct_stock(txn, self.owner)
        item.refresh_from_db()
        self.assertIsNone(item.unit_cost)

    def test_stamping_the_cost_does_not_add_audit_rows_or_touch_the_subtotal(self):
        from audit.models import AuditLog

        self.batch(10, procurement=self.po('12.00'))
        self.floor_stock(10)
        txn, item = self.sale(4)
        before = AuditLog.objects.filter(entity='sales.SalesItem').count()
        _deduct_stock(txn, self.owner)
        self.assertEqual(AuditLog.objects.filter(entity='sales.SalesItem').count(), before)
        item.refresh_from_db()
        self.assertEqual(item.subtotal, D('200.00'))

    def test_an_online_order_is_costed_when_it_is_handed_over_not_when_placed(self):
        from billing.models import BillingRecord

        self.batch(10, procurement=self.po('12.00'))
        self.floor_stock(10)
        txn, item = self.sale(3, status=SalesTransaction.OrderStatus.PREPARING)
        SalesTransaction.objects.filter(pk=txn.pk).update(transaction_type='Online', fulfillment_type='Pickup')
        BillingRecord.objects.filter(transaction=txn).update(
            payment_method=BillingRecord.PaymentMethod.COD, status=BillingRecord.PaymentStatus.PAID)
        item.refresh_from_db()
        self.assertIsNone(item.unit_cost)                         # placed, not yet taken off the shelf

        client = Client()
        client.force_login(self.owner)
        response = client.post(reverse('transaction_confirm_receipt', args=[txn.pk]), {
            'received_by_name': 'Juana Cruz', 'received_at': '', 'notes': '', 'signature_data': ''})
        self.assertEqual(response.status_code, 302)
        txn.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(txn.order_status, SalesTransaction.OrderStatus.COMPLETED)
        self.assertEqual(item.unit_cost, D('12.0000'))


class PurchaseCostFollowsDeliveriesTests(CostFixture):
    def test_receiving_a_delivery_updates_the_products_latest_cost(self):
        from inventory.models import Warehouse

        self.assertEqual(Warehouse.objects.filter(branch=self.branch).count(), 1)
        po = self.po('13.50', received=0, status=Procurement.Status.ORDERED)
        item = po.items.get()
        record_delivery(po, self.owner, lines={item.pk: (10, None)}, receipt_date=self.today)
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, D('13.50'))
        # The lot it created resolves to that price.
        lot = Batch.objects.get(procurement=po)
        self.assertEqual(costing.CostResolver().batch_cost(lot), D('13.50'))

    def test_the_next_delivery_at_a_new_price_becomes_the_latest(self):
        first = self.po('13.50', received=0)
        record_delivery(first, self.owner, lines={first.items.get().pk: (5, None)}, receipt_date=self.today)
        second = self.po('15.00', received=0)
        record_delivery(second, self.owner, lines={second.items.get().pk: (5, None)}, receipt_date=self.today)
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, D('15.00'))

    def test_a_line_with_no_agreed_price_leaves_the_cost_alone(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('8.00'))
        po = self.po(None, received=0)
        record_delivery(po, self.owner, lines={po.items.get().pk: (5, None)}, receipt_date=self.today)
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, D('8.00'))

    def test_note_purchase_cost_ignores_unusable_prices(self):
        self.assertFalse(costing.note_purchase_cost(self.product, None))
        self.assertFalse(costing.note_purchase_cost(self.product, D('0')))
        self.assertFalse(costing.note_purchase_cost(None, D('5')))
        self.assertTrue(costing.note_purchase_cost(self.product, D('5')))
        self.assertFalse(costing.note_purchase_cost(self.product, D('5')))     # unchanged


class BackfillTests(CostFixture):
    def completed(self, qty, **kw):
        return self.sale(qty, **kw)

    def test_old_lines_get_the_cost_of_the_lots_they_drew_on(self):
        batch = self.batch(10, procurement=self.po('12.00'))
        txn, item = self.completed(4)
        SalesItemBatchConsumption.objects.create(sales_item=item, batch=batch, quantity_taken=4)
        filled, unknown = costing.backfill_sale_costs()
        item.refresh_from_db()
        self.assertEqual((filled, unknown, item.unit_cost), (1, 0, D('12.0000')))

    def test_a_line_with_no_consumption_record_uses_the_product_cost(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('9.00'))
        txn, item = self.completed(4)
        costing.backfill_sale_costs()
        item.refresh_from_db()
        self.assertEqual(item.unit_cost, D('9.0000'))

    def test_a_frozen_cost_is_never_overwritten(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('9.00'))
        txn, item = self.completed(4)
        SalesItem.objects.filter(pk=item.pk).update(unit_cost=D('5.5000'))
        self.assertEqual(costing.backfill_sale_costs(), (0, 0))
        item.refresh_from_db()
        self.assertEqual(item.unit_cost, D('5.5000'))

    def test_pending_and_cancelled_orders_are_left_alone(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('9.00'))
        _t1, pending = self.sale(1, status=SalesTransaction.OrderStatus.PENDING)
        _t2, cancelled = self.sale(1, status=SalesTransaction.OrderStatus.CANCELLED)
        self.assertEqual(costing.backfill_sale_costs(), (0, 0))
        pending.refresh_from_db()
        cancelled.refresh_from_db()
        self.assertIsNone(pending.unit_cost)
        self.assertIsNone(cancelled.unit_cost)

    def test_lines_that_cannot_be_costed_are_counted_not_zeroed(self):
        txn, item = self.completed(4)
        self.assertEqual(costing.backfill_sale_costs(), (0, 1))
        item.refresh_from_db()
        self.assertIsNone(item.unit_cost)

    def test_dry_run_changes_nothing(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('9.00'))
        txn, item = self.completed(4)
        self.assertEqual(costing.backfill_sale_costs(dry_run=True), (1, 0))
        item.refresh_from_db()
        self.assertIsNone(item.unit_cost)

    def test_the_command_reports_and_fills(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('9.00'))
        txn, item = self.completed(4)
        out = StringIO()
        call_command('backfill_sale_costs', '--dry-run', stdout=out)
        self.assertIn('Would fill 1 sale line', out.getvalue())
        call_command('backfill_sale_costs', stdout=out)
        item.refresh_from_db()
        self.assertEqual(item.unit_cost, D('9.0000'))


class ProductBackfillMigrationTests(CostFixture):
    def run_backfill(self):
        migration = importlib.import_module('inventory.migrations.0037_backfill_product_cost_price')
        migration.backfill(django_apps, None)
        self.product.refresh_from_db()

    def test_the_latest_delivered_purchase_price_wins(self):
        old = self.po('10.00')
        Procurement.objects.filter(pk=old.pk).update(order_date=self.today - dt.timedelta(days=30))
        self.po('14.00')
        self.run_backfill()
        self.assertEqual(self.product.cost_price, D('14.00'))

    def test_an_order_that_delivered_nothing_is_ignored_in_favour_of_the_supplier_offer(self):
        self.po('99.00', received=0)
        SupplierProduct.objects.create(
            supplier=self.supplier, product=self.product, current_cost_price=D('11.00'),
            status=SupplierProduct.Status.APPROVED)
        self.run_backfill()
        self.assertEqual(self.product.cost_price, D('11.00'))

    def test_a_preferred_offer_beats_a_cheaper_one_and_proposed_offers_are_ignored(self):
        other = User.objects.create_user(username='cost-sup2', password='pw', role=User.Role.SUPPLIER)
        third = User.objects.create_user(username='cost-sup3', password='pw', role=User.Role.SUPPLIER)
        SupplierProduct.objects.create(
            supplier=self.supplier, product=self.product, current_cost_price=D('11.00'), is_preferred=True)
        SupplierProduct.objects.create(supplier=other, product=self.product, current_cost_price=D('8.00'))
        SupplierProduct.objects.create(
            supplier=third, product=self.product, current_cost_price=D('1.00'),
            status=SupplierProduct.Status.PROPOSED)
        self.run_backfill()
        self.assertEqual(self.product.cost_price, D('11.00'))

    def test_a_cost_someone_typed_is_not_overwritten_and_no_data_means_blank(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('7.00'))
        self.po('14.00')
        self.run_backfill()
        self.assertEqual(self.product.cost_price, D('7.00'))
        bare = Product.objects.create(sku_code='CO-2', product_name='Nothing known', unit_price=D('5'))
        self.run_backfill()
        bare.refresh_from_db()
        self.assertIsNone(bare.cost_price)


class ProductFormTests(CostFixture):
    def post(self, url, **extra):
        data = {'product_name': 'Canned Tuna', 'unit_price': '50', 'unit_of_measure': 'Piece',
                'category': self.product.category_id}
        data.update(extra)
        client = Client()
        client.force_login(self.owner)
        return client.post(url, data)

    def edit(self, **extra):
        return self.post(reverse('product_edit', args=[self.product.pk]), **extra)

    def test_the_owner_can_set_change_and_clear_the_cost_price(self):
        self.edit(cost_price='31.50')
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, D('31.50'))
        self.edit(cost_price='')
        self.product.refresh_from_db()
        self.assertIsNone(self.product.cost_price)

    def test_leaving_the_field_out_of_the_request_keeps_the_cost(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('31.50'))
        self.edit()
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_price, D('31.50'))

    def test_bad_costs_are_refused_and_the_form_says_why(self):
        for bad, message in (('0', 'Cost price must be more than ₱0.'), ('-4', 'Cost price must be more than ₱0.'),
                             ('abc', 'Cost price must be a valid number.'),
                             ('250000.01', 'Cost price cannot be more than ₱250,000.00.')):
            response = self.edit(cost_price=bad)
            self.assertContains(response, message)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.cost_price)

    def test_a_product_can_be_added_with_a_cost(self):
        response = self.post(reverse('product_add'), product_name='New Item', cost_price='12.25')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Product.objects.get(product_name='New Item').cost_price, D('12.25'))

    def test_the_edit_page_shows_the_current_cost(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('31.50'))
        client = Client()
        client.force_login(self.owner)
        response = client.get(reverse('product_edit', args=[self.product.pk]))
        self.assertContains(response, 'name="cost_price"')
        self.assertContains(response, 'value="31.50"')

    def test_the_cost_is_not_exposed_through_the_public_api(self):
        Product.objects.filter(pk=self.product.pk).update(cost_price=D('31.50'))
        rows = Client().get('/api/products/').json()
        self.assertEqual([row['name'] for row in rows], ['Canned Tuna'])
        self.assertFalse([key for key in rows[0] if 'cost' in key.lower()])
        self.assertNotIn('31.5', str(rows))


class ApiSaleIsCostedTests(CostFixture):
    """A walk-in sale posted to /api/sales/ takes its stock at creation, so its
    lines are costed there -- the second of the two places stock leaves the shelf."""

    def test_a_physical_sale_over_the_api_freezes_the_cost_of_its_lots(self):
        self.batch(10, procurement=self.po('12.00'))
        self.floor_stock(10)
        client = Client()
        client.force_login(self.owner)
        response = client.post('/api/sales/', {
            'branchId': self.branch.pk, 'transactionType': 'Physical', 'modeOfPayment': 'Cash',
            'items': [{'productId': self.product.pk, 'quantity': 3}],
        }, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        item = SalesItem.objects.get()
        self.assertEqual(item.unit_cost, D('12.0000'))
        self.assertEqual(item.quantity_sold, 3)

    def test_an_online_order_over_the_api_is_not_costed_until_it_ships(self):
        self.batch(10, procurement=self.po('12.00'))
        self.floor_stock(10)
        shopper = User.objects.create_user(username='cost-shopper', password='pw', role=User.Role.CUSTOMER)
        client = Client()
        client.force_login(shopper)
        response = client.post('/api/sales/', {
            'branchId': self.branch.pk, 'transactionType': 'Online', 'modeOfPayment': 'Cash on Delivery',
            'deliveryAddress': '12 Real St, Baliuag',
            'items': [{'productId': self.product.pk, 'quantity': 2}],
        }, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(SalesItem.objects.get().unit_cost)
