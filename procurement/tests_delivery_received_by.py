"""P06 — every receiving event records who received it (the signed-in user),
optionally who delivered it, on both the template view and the API."""
import datetime as dt
import json

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, User
from inventory.models import Warehouse
from procurement.models import DeliveryReceipt, Procurement, ProcurementItem
from procurement.tests import ProcurementAuthorizationBase


class ReceivedByTests(ProcurementAuthorizationBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Warehouse.objects.get_or_create(
            branch=cls.home_branch, defaults={'name': 'P06 Warehouse'})

    def setUp(self):
        super().setUp()
        self.po = self._po(
            status=Procurement.Status.INCOMING, delivery_date=dt.date(2026, 2, 10))
        self.item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product, quantity_ordered=10)

    def _receive(self, user, quantity, **extra):
        self.client.force_login(user)
        data = {
            'item_id': [str(self.item.pk)], 'quantity_received': [str(quantity)],
            'actual_receipt_date': '2026-02-11',
        }
        data.update(extra)
        return self.client.post(reverse('delivery_receive', args=[self.po.pk]), data)

    def test_receipt_records_the_signed_in_user_not_a_posted_name(self):
        self._receive(self.manager, 4, received_by='someone-else', delivered_by='Kuya Ben')
        receipt = DeliveryReceipt.objects.get()
        self.assertEqual(receipt.received_by, self.manager)
        self.assertEqual(receipt.delivered_by, 'Kuya Ben')
        self.assertEqual(str(receipt.receipt_date), '2026-02-11')
        self.assertEqual(receipt.procurement, self.po)

    def test_each_shipment_gets_its_own_receipt(self):
        self._receive(self.manager, 4)
        self._receive(self.owner, 6)
        receipts = list(self.po.receipts.all())
        self.assertEqual([r.received_by for r in receipts], [self.manager, self.owner])

    def test_a_rejected_receipt_records_nothing(self):
        self._receive(self.manager, 0)  # "enter at least one quantity" error
        self.assertFalse(DeliveryReceipt.objects.exists())

    def test_delivered_by_is_optional_and_trimmed(self):
        self._receive(self.owner, 2, delivered_by='  ' + 'x' * 150)
        self.assertEqual(len(DeliveryReceipt.objects.get().delivered_by), 100)
        self.po.refresh_from_db()

    def test_detail_page_lists_who_received_it(self):
        self._receive(self.manager, 4, delivered_by='Kuya Ben')
        response = self.client.get(reverse('procurement_detail', args=[self.po.pk]))
        self.assertContains(response, 'c1-manager')
        self.assertContains(response, 'Kuya Ben')

    def test_printable_po_names_the_receiver(self):
        self._receive(self.manager, 4)
        response = self.client.get(reverse('procurement_print', args=[self.po.pk]))
        self.assertContains(response, 'Received into stock by c1-manager')

    def test_receive_modal_shows_the_receiving_user(self):
        self.client.force_login(self.manager)
        response = self.client.get(f"{reverse('delivery_list')}?receive={self.po.pk}")
        self.assertContains(response, 'name="delivered_by"')
        self.assertContains(response, 'c1-manager')

    def test_api_receipt_records_the_authenticated_user(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            f'/api/procurement/{self.po.pk}/receive/',
            data=json.dumps({
                'outcome': DeliveryReceipt.Outcome.ACCEPTED,
                'submissionKey': 'received-by-api',
                'actualReceiptDate': '2026-02-11',
                'deliveredBy': 'API Courier',
                'items': [{'itemId': self.item.pk, 'quantityReceived': 4}],
            }),
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        receipt = DeliveryReceipt.objects.get()
        self.assertEqual(receipt.received_by, self.owner)
        self.assertEqual(receipt.delivered_by, 'API Courier')
        self.assertEqual(response.json()['receipts'][0]['receivedBy'], 'c1-owner')

    def test_a_receipt_survives_its_receiver_being_deleted(self):
        self._receive(self.manager, 4)
        self.manager.delete()
        receipt = DeliveryReceipt.objects.get()
        self.assertIsNone(receipt.received_by)
        self.assertEqual(receipt.received_by_name, '')
        # ...and the pages that print it still render.
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(reverse('procurement_detail', args=[self.po.pk])).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('procurement_print', args=[self.po.pk])).status_code, 200)


class ReceiptBackfillMigrationTests(TransactionTestCase):
    """0010 rebuilds receipts for POs received before this feature existed,
    from the stock movements those receipts wrote."""

    # The other apps are pinned at their newest migration so the historical
    # models below match the real table shapes (e.g. User's NOT NULL columns).
    _others = [('accounts', '0024_supplier_category_fk'),
               ('branches', '0002_branch_structured_location'),
               ('inventory', '0027_enforce_positive_prices')]
    before = [('procurement', '0009_supplier_offer_workflow')] + _others
    after = [('procurement', '0010_delivery_receipt')] + _others

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_received_orders_get_one_receipt_per_user_and_day(self):
        old = self._migrate(self.before)
        User_ = old.get_model('accounts', 'User')
        Branch_ = old.get_model('branches', 'Branch')
        Proc = old.get_model('procurement', 'Procurement')
        Movement = old.get_model('inventory', 'StockMovement')
        Product_ = old.get_model('inventory', 'Product')
        Warehouse_ = old.get_model('inventory', 'Warehouse')
        WStock = old.get_model('inventory', 'WarehouseStock')

        owner = User_.objects.create(
            username='bf-owner', role='Owner', password='x')
        branch = Branch_.objects.create(branch_name='BF', location='X', owner=owner)
        product = Product_.objects.create(sku_code='BF-1', product_name='BF', unit_price=5)
        wh = Warehouse_.objects.create(branch=branch, name='BF WH')
        ws = WStock.objects.create(product=product, warehouse=wh)
        received = Proc.objects.create(
            branch=branch, order_date=dt.date(2026, 1, 1), status='Received', total_cost=0,
            po_number='BF-PO-1')
        untouched = Proc.objects.create(
            branch=branch, order_date=dt.date(2026, 1, 1), status='Incoming', total_cost=0,
            po_number='BF-PO-2')
        for _ in range(2):  # two movements, same user and day -> one receipt
            Movement.objects.create(
                warehouse_stock=ws, location_type='Warehouse',
                movement_type='Procurement Received', quantity_change=1,
                quantity_before=0, quantity_after=1, reference_id=received.pk,
                created_by=owner)

        new = self._migrate(self.after)
        Receipt = new.get_model('procurement', 'DeliveryReceipt')
        self.assertEqual(Receipt.objects.filter(procurement_id=received.pk).count(), 1)
        self.assertEqual(Receipt.objects.get(procurement_id=received.pk).received_by_id, owner.pk)
        self.assertFalse(Receipt.objects.filter(procurement_id=untouched.pk).exists())
