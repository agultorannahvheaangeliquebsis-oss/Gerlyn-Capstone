"""The offline queue's server half: every queued change is applied exactly
once, held to the same rules as the online form, and never reported as saved
unless it demonstrably was.

Most tests drive POST /offline/sync/ the way static/js/offline-sync.js does:
multipart, the form's own fields plus a JSON `_offline` envelope.
"""
import datetime as dt
import json
import uuid
from decimal import Decimal

from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import StaffProfile, User
from branches.models import Branch
from core.module_permissions import ADD, DEFAULT_STAFF_MODULES, PRODUCTS
from inventory.models import (
    Batch, Inventory, Product, StockMovement, StockTransfer, Warehouse, WarehouseStock,
)
from procurement.models import DeliveryReceipt, Procurement, ProcurementItem
from procurement.tests import ProcurementAuthorizationBase
from sales.models import SalesTransaction

from .models import OfflineOperation


def sync_post(client, user, op_type, fields, *, op_id=None, target_pk=None,
              meta=None, client_user_id=None):
    op_id = op_id or uuid.uuid4().hex
    envelope = {
        'client_op_id': op_id,
        'op_type': op_type,
        'client_user_id': user.pk if client_user_id is None else client_user_id,
        'queued_at': '2026-10-12T08:00:00+08:00',
        'meta': meta or {},
    }
    if target_pk is not None:
        envelope['target_pk'] = target_pk
    body = dict(fields)
    body['_offline'] = json.dumps(envelope)
    return client.post(reverse('offline_sync_apply'), body), op_id


class _SalesFixture:
    def build(self):
        self.owner = User.objects.create_user(
            username='of-owner', password='pw', role=User.Role.OWNER)
        self.branch = Branch.objects.create(
            branch_name='Offline Branch', location='Baliwag', owner=self.owner)
        self.other_branch = Branch.objects.create(
            branch_name='Other Branch', location='Tiaong', owner=self.owner)
        self.product = Product.objects.create(
            sku_code='OFF-1', product_name='Cooking Oil',
            unit_of_measure='Bottle', unit_price=Decimal('100.00'))
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=10, reorder_level=2)
        self.staff = User.objects.create_user(
            username='of-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=self.staff, defaults={'staff_name': 'Sam', 'branch': self.branch})
        self.staff.allowed_modules = list(DEFAULT_STAFF_MODULES)
        self.staff.save(update_fields=['allowed_modules'])

    def stock(self):
        return Inventory.objects.get(product=self.product, branch=self.branch).quantity_on_hand

    def sale_fields(self, qty=1, tendered='500'):
        return {
            'mode_of_payment': 'Cash', 'amount_tendered': tendered,
            'product_id': [str(self.product.pk)], 'quantity': [str(qty)],
        }


class PingTests(_SalesFixture, TestCase):
    def setUp(self):
        self.build()

    def test_anonymous_ping_says_signed_out_and_leaks_nothing(self):
        data = self.client.get(reverse('offline_ping')).json()
        self.assertTrue(data['ok'])
        self.assertFalse(data['authenticated'])
        self.assertNotIn('csrf', data)
        self.assertNotIn('user_id', data)

    def test_signed_in_ping_names_the_user_and_the_allow_list(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('offline_ping'))
        data = resp.json()
        self.assertTrue(data['authenticated'])
        self.assertEqual(data['user_id'], self.staff.pk)
        self.assertIn('sale.walkin', data['operations'])
        self.assertTrue(data['csrf'])
        self.assertIn('no-store', resp['Cache-Control'])

    def test_customer_is_not_offered_the_queue(self):
        customer = User.objects.create_user(
            username='of-cust', password='pw', role=User.Role.CUSTOMER)
        self.client.force_login(customer)
        self.assertFalse(self.client.get(reverse('offline_ping')).json()['authenticated'])


class SaleSyncTests(_SalesFixture, TestCase):
    def setUp(self):
        self.build()
        self.client.force_login(self.staff)

    def test_queued_sale_is_applied_through_the_real_view(self):
        resp, op_id = sync_post(self.client, self.staff, 'sale.walkin', self.sale_fields(qty=2))
        data = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['status'], 'applied', data)
        txn = SalesTransaction.objects.get()
        self.assertEqual(data['result']['label'], txn.order_number)
        self.assertEqual(data['result']['url'], reverse('transaction_detail', args=[txn.pk]))
        self.assertEqual(txn.recorded_by, self.staff)
        self.assertEqual(txn.branch, self.branch)
        self.assertEqual(txn.total_amount, Decimal('200.00'))
        self.assertEqual(self.stock(), 8)
        self.assertTrue(StockMovement.objects.filter(reference_id=txn.pk).exists())
        op = OfflineOperation.objects.get(client_op_id=op_id)
        self.assertEqual(op.status, OfflineOperation.Status.APPLIED)
        self.assertIsNotNone(op.applied_at)

    def test_resending_an_applied_change_never_applies_it_twice(self):
        fields = self.sale_fields(qty=3)
        first, op_id = sync_post(self.client, self.staff, 'sale.walkin', fields)
        again, _ = sync_post(self.client, self.staff, 'sale.walkin', fields, op_id=op_id)
        self.assertEqual(first.json()['status'], 'applied')
        self.assertEqual(again.json()['status'], 'applied')
        self.assertTrue(again.json()['duplicate'])
        self.assertEqual(again.json()['result'], first.json()['result'])
        self.assertEqual(SalesTransaction.objects.count(), 1)
        self.assertEqual(self.stock(), 7)

    def test_two_distinct_sales_are_both_applied(self):
        fields = self.sale_fields(qty=1)
        sync_post(self.client, self.staff, 'sale.walkin', fields)
        sync_post(self.client, self.staff, 'sale.walkin', fields)
        self.assertEqual(SalesTransaction.objects.count(), 2)
        self.assertEqual(self.stock(), 8)

    def test_oversold_sale_is_rejected_leaves_no_trace_and_can_be_retried(self):
        fields = self.sale_fields(qty=25, tendered='5000')
        resp, op_id = sync_post(self.client, self.staff, 'sale.walkin', fields)
        data = resp.json()
        self.assertEqual(data['status'], 'rejected', data)
        self.assertIn('Not enough stock', data['message'])
        self.assertFalse(SalesTransaction.objects.exists())
        self.assertEqual(self.stock(), 10)
        op = OfflineOperation.objects.get(client_op_id=op_id)
        self.assertEqual(op.status, OfflineOperation.Status.REJECTED)

        # A delivery lands; the very same queued sale is now fine.
        Inventory.objects.filter(product=self.product, branch=self.branch).update(
            quantity_on_hand=30)
        Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=30, received_date=dt.date.today())
        retry, _ = sync_post(self.client, self.staff, 'sale.walkin', fields, op_id=op_id)
        self.assertEqual(retry.json()['status'], 'applied', retry.json())
        self.assertEqual(SalesTransaction.objects.count(), 1)

    def test_validation_errors_come_back_as_reasons_not_success(self):
        resp, _ = sync_post(self.client, self.staff, 'sale.walkin', {
            'mode_of_payment': 'Cash', 'amount_tendered': '10',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
        })
        data = resp.json()
        self.assertEqual(data['status'], 'rejected')
        self.assertIn('less than the amount due', data['message'])
        self.assertFalse(SalesTransaction.objects.exists())

    def test_same_id_with_different_data_is_refused(self):
        first, op_id = sync_post(self.client, self.staff, 'sale.walkin', self.sale_fields(qty=1))
        second, _ = sync_post(
            self.client, self.staff, 'sale.walkin', self.sale_fields(qty=4), op_id=op_id)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()['status'], 'id_reused')
        self.assertEqual(SalesTransaction.objects.count(), 1)

    def test_staff_is_pinned_to_their_own_branch_whatever_the_form_says(self):
        fields = self.sale_fields()
        fields['branch'] = str(self.other_branch.pk)
        resp, _ = sync_post(self.client, self.staff, 'sale.walkin', fields)
        self.assertEqual(resp.json()['status'], 'applied')
        self.assertEqual(SalesTransaction.objects.get().branch, self.branch)

    def test_module_permission_is_enforced_for_queued_changes(self):
        self.staff.allowed_modules = []
        self.staff.save(update_fields=['allowed_modules'])
        resp, _ = sync_post(self.client, self.staff, 'sale.walkin', self.sale_fields())
        data = resp.json()
        self.assertEqual(data['status'], 'rejected', data)
        self.assertFalse(SalesTransaction.objects.exists())
        self.assertEqual(self.stock(), 10)

    def test_a_queue_cannot_be_applied_by_a_different_signed_in_user(self):
        resp, _ = sync_post(
            self.client, self.staff, 'sale.walkin', self.sale_fields(),
            client_user_id=self.owner.pk)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['status'], 'wrong_user')
        self.assertFalse(SalesTransaction.objects.exists())

    def test_an_operation_id_is_scoped_to_its_user(self):
        op_id = uuid.uuid4().hex
        sync_post(self.client, self.staff, 'sale.walkin', self.sale_fields(), op_id=op_id)
        owner_client = Client()
        owner_client.force_login(self.owner)
        fields = self.sale_fields()
        fields['branch'] = str(self.branch.pk)
        resp, _ = sync_post(owner_client, self.owner, 'sale.walkin', fields, op_id=op_id)
        self.assertEqual(resp.json()['status'], 'applied')
        self.assertEqual(SalesTransaction.objects.count(), 2)


class GateTests(_SalesFixture, TestCase):
    def setUp(self):
        self.build()

    def test_anonymous_sync_is_401_not_a_login_redirect(self):
        resp, _ = sync_post(self.client, self.staff, 'sale.walkin', self.sale_fields())
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()['status'], 'unauthenticated')
        self.assertFalse(SalesTransaction.objects.exists())

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        resp, _ = sync_post(client, self.staff, 'sale.walkin', self.sale_fields())
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(SalesTransaction.objects.exists())

    def test_get_is_not_allowed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('offline_sync_apply')).status_code, 405)

    def test_sensitive_actions_are_not_in_the_allow_list(self):
        self.client.force_login(self.owner)
        for op_type in (
            'procurement.approve', 'billing.confirm_payment', 'sales.refund',
            'sales.refund_approve', 'accounts.user_edit', 'inventory.transfer_approve',
            'sales.void_order', '../transaction_set_status', '',
        ):
            resp, _ = sync_post(self.client, self.owner, op_type, {'x': '1'})
            self.assertEqual(resp.status_code, 400, op_type)
            self.assertIn(resp.json()['status'], ('online_only', 'invalid'), op_type)
        self.assertFalse(OfflineOperation.objects.exists())

    def test_malformed_envelope_and_ids_are_refused(self):
        self.client.force_login(self.staff)
        url = reverse('offline_sync_apply')
        self.assertEqual(self.client.post(url, {'_offline': 'not json'}).status_code, 400)
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        resp, _ = sync_post(
            self.client, self.staff, 'sale.walkin', self.sale_fields(), op_id='short')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SalesTransaction.objects.exists())

    def test_customers_and_suppliers_cannot_sync(self):
        customer = User.objects.create_user(
            username='of-cust2', password='pw', role=User.Role.CUSTOMER)
        self.client.force_login(customer)
        resp, _ = sync_post(self.client, customer, 'sale.walkin', self.sale_fields())
        self.assertEqual(resp.status_code, 403)

    def test_owner_cannot_queue_a_purchase_order_it_is_an_authorization(self):
        self.client.force_login(self.owner)
        resp, _ = sync_post(self.client, self.owner, 'procurement.create_po', {
            'order_date': '2026-10-12', 'supplier': '1',
            'product_id': ['1'], 'quantity_ordered': ['1'], 'funding_source': 'x',
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Procurement.objects.exists())


class CountCorrectionConflictTests(_SalesFixture, TestCase):
    def setUp(self):
        self.build()
        self.manager = User.objects.create_user(
            username='of-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        self.manager.manager_profile.branch = self.branch
        self.manager.manager_profile.save(update_fields=['branch'])
        Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=10, received_date=dt.date.today())
        self.client.force_login(self.manager)

    def _fields(self, counted):
        return {
            'product_id': str(self.product.pk), 'branch_id': str(self.branch.pk),
            'counted_quantity': str(counted), 'reason': 'Shelf count',
        }

    def test_count_against_unchanged_stock_is_applied(self):
        resp, _ = sync_post(
            self.client, self.manager, 'inventory.count_correction', self._fields(7),
            meta={'base_quantity': 10})
        self.assertEqual(resp.json()['status'], 'applied', resp.json())
        self.assertEqual(self.stock(), 7)

    def test_count_is_a_conflict_when_stock_moved_while_offline(self):
        Inventory.objects.filter(product=self.product, branch=self.branch).update(
            quantity_on_hand=4)
        resp, op_id = sync_post(
            self.client, self.manager, 'inventory.count_correction', self._fields(7),
            meta={'base_quantity': 10})
        data = resp.json()
        self.assertEqual(data['status'], 'conflict', data)
        self.assertEqual(data['current']['quantity_on_hand'], 4)
        self.assertIn('changed while you were offline', data['message'])
        self.assertEqual(self.stock(), 4)
        self.assertEqual(
            OfflineOperation.objects.get(client_op_id=op_id).status,
            OfflineOperation.Status.CONFLICT)
        self.assertFalse(StockMovement.objects.exists())

    def test_count_without_a_base_quantity_is_refused_not_guessed(self):
        resp, _ = sync_post(
            self.client, self.manager, 'inventory.count_correction', self._fields(7))
        self.assertEqual(resp.json()['status'], 'conflict')
        self.assertEqual(self.stock(), 10)

    def test_staff_cannot_queue_a_count_correction(self):
        self.client.force_login(self.staff)
        resp, _ = sync_post(
            self.client, self.staff, 'inventory.count_correction', self._fields(7),
            meta={'base_quantity': 10})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.stock(), 10)

    def test_other_branch_quantity_is_not_revealed_by_a_conflict(self):
        Inventory.objects.create(
            product=self.product, branch=self.other_branch,
            quantity_on_hand=99, reorder_level=0)
        fields = self._fields(7)
        fields['branch_id'] = str(self.other_branch.pk)
        resp, _ = sync_post(
            self.client, self.manager, 'inventory.count_correction', fields,
            meta={'base_quantity': 0})
        # The manager is pinned to their own branch; the other branch's
        # figure (99) never appears in the answer. Compare the quantities, not
        # a substring of the whole body: the random operation id can contain "99".
        body = resp.json()
        shown = body.get('message', '') + json.dumps(body.get('current') or {})
        self.assertNotRegex(shown, r'99')


class RefillSyncTests(TransactionTestCase):
    """TransactionTestCase: the Owner's auto-approval takes row locks, and the
    real endpoint runs in autocommit -- see feedback on TestCase's blind spot."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='of-rf-owner', password='pw', role=User.Role.OWNER)
        self.branch = Branch.objects.create(
            owner=self.owner, branch_name='Savers', location='S')
        self.product = Product.objects.create(
            sku_code='RF-1', product_name='Tuna', unit_price='30.00')
        WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=self.product, quantity_on_hand=16)
        Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.WAREHOUSE,
            quantity=16, received_date=dt.date.today(), expiry_date=dt.date(2030, 1, 1))
        self.client.force_login(self.owner)

    def _fields(self, qty):
        return {
            'branch_id': str(self.branch.pk), 'source_branch_id': str(self.branch.pk),
            'product_id': [str(self.product.pk)], 'quantity_requested': [str(qty)],
        }

    def test_queued_refill_request_is_created_once_and_moves_no_stock(self):
        first, op_id = sync_post(
            self.client, self.owner, 'inventory.floor_refill', self._fields(5))
        again, _ = sync_post(
            self.client, self.owner, 'inventory.floor_refill', self._fields(5), op_id=op_id)
        self.assertEqual(first.json()['status'], 'applied', first.json())
        self.assertTrue(again.json()['duplicate'])
        transfer = StockTransfer.objects.get()
        self.assertEqual(first.json()['result']['label'], transfer.slip_number)
        self.assertEqual(
            WarehouseStock.objects.get(product=self.product).quantity_on_hand, 16)
        self.assertFalse(StockMovement.objects.exists())

    def test_request_beyond_warehouse_stock_is_rejected_and_nothing_is_saved(self):
        resp, _ = sync_post(
            self.client, self.owner, 'inventory.floor_refill', self._fields(50))
        self.assertEqual(resp.json()['status'], 'rejected', resp.json())
        self.assertIn('Insufficient warehouse stock', resp.json()['message'])
        self.assertFalse(StockTransfer.objects.exists())


class ProcurementSyncTests(ProcurementAuthorizationBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.warehouse, _ = Warehouse.objects.get_or_create(
            branch=cls.home_branch, defaults={'name': 'Offline Warehouse'})
        cls.staff = User.objects.create_user(
            username='of-p-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=cls.staff,
            defaults={'branch': cls.home_branch, 'staff_name': 'Sam', 'contact_number': '0900'})
        cls.staff.allowed_modules = list(DEFAULT_STAFF_MODULES)
        cls.staff.save(update_fields=['allowed_modules'])

    def _receive_fields(self, item, qty):
        return {
            'item_id': [str(item.pk)], 'quantity_received': [str(qty)],
            'actual_receipt_date': dt.date.today().isoformat(),
        }

    def _incoming_po(self, qty=10, branch=None):
        po = self._po(
            branch=branch, status=Procurement.Status.INCOMING,
            delivery_date=dt.date.today())
        item = ProcurementItem.objects.create(
            procurement=po, product=self.product, quantity_ordered=qty)
        return po, item

    def test_queued_delivery_receipt_credits_the_warehouse_exactly_once(self):
        po, item = self._incoming_po(10)
        self.client.force_login(self.staff)
        first, op_id = sync_post(
            self.client, self.staff, 'procurement.receive_delivery',
            self._receive_fields(item, 10), target_pk=po.pk)
        again, _ = sync_post(
            self.client, self.staff, 'procurement.receive_delivery',
            self._receive_fields(item, 10), target_pk=po.pk, op_id=op_id)
        self.assertEqual(first.json()['status'], 'applied', first.json())
        self.assertTrue(again.json()['duplicate'])
        self.assertEqual(first.json()['result']['label'], po.po_number)
        self.assertEqual(DeliveryReceipt.objects.filter(procurement=po).count(), 1)
        self.assertEqual(
            WarehouseStock.objects.get(product=self.product).quantity_on_hand, 10)
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.RECEIVED)

    def test_receipt_for_a_po_someone_else_already_received_is_not_reported_as_synced(self):
        po, item = self._incoming_po(10)
        # Receiver A is online and finishes the delivery.
        self.client.force_login(self.manager)
        self.client.post(
            reverse('delivery_receive', args=[po.pk]), self._receive_fields(item, 10))
        # Receiver B had the same PO open offline and now syncs.
        self.client.force_login(self.staff)
        resp, _ = sync_post(
            self.client, self.staff, 'procurement.receive_delivery',
            self._receive_fields(item, 10), target_pk=po.pk)
        self.assertIn(resp.json()['status'], ('conflict', 'rejected'), resp.json())
        self.assertNotEqual(resp.json()['status'], 'applied')
        self.assertEqual(DeliveryReceipt.objects.filter(procurement=po).count(), 1)
        self.assertEqual(
            WarehouseStock.objects.get(product=self.product).quantity_on_hand, 10)

    def test_over_receipt_is_rejected_and_writes_nothing(self):
        po, item = self._incoming_po(10)
        self.client.force_login(self.staff)
        resp, _ = sync_post(
            self.client, self.staff, 'procurement.receive_delivery',
            self._receive_fields(item, 99), target_pk=po.pk)
        self.assertEqual(resp.json()['status'], 'rejected', resp.json())
        self.assertFalse(DeliveryReceipt.objects.exists())
        self.assertFalse(WarehouseStock.objects.filter(product=self.product).exists())

    def test_receipt_for_another_branchs_po_is_refused(self):
        po, item = self._incoming_po(10, branch=self.other_branch)
        self.client.force_login(self.staff)
        resp, _ = sync_post(
            self.client, self.staff, 'procurement.receive_delivery',
            self._receive_fields(item, 5), target_pk=po.pk)
        self.assertEqual(resp.json()['status'], 'rejected', resp.json())
        self.assertFalse(DeliveryReceipt.objects.exists())

    def test_receipt_without_a_target_is_invalid(self):
        self.client.force_login(self.staff)
        resp, _ = sync_post(
            self.client, self.staff, 'procurement.receive_delivery', {'item_id': ['1']})
        self.assertEqual(resp.status_code, 400)

    def test_manager_po_request_is_created_once_as_a_draft(self):
        self.client.force_login(self.manager)
        fields = {
            'order_date': dt.date.today().isoformat(),
            'supplier': str(self.supplier.pk),
            'product_id': [str(self.product.pk)], 'quantity_ordered': ['20'],
            'funding_source': 'Bank Transfer',
        }
        first, op_id = sync_post(
            self.client, self.manager, 'procurement.create_po', fields)
        data = first.json()
        if data['status'] == 'rejected':
            self.fail(f'PO form rejected: {data}')
        again, _ = sync_post(
            self.client, self.manager, 'procurement.create_po', fields, op_id=op_id)
        self.assertEqual(data['status'], 'applied', data)
        self.assertTrue(again.json()['duplicate'])
        po = Procurement.objects.get()
        self.assertEqual(po.status, Procurement.Status.DRAFT)
        self.assertEqual(po.submission_key, op_id)
        self.assertEqual(po.created_by, self.manager)
        self.assertEqual(data['result']['label'], po.po_number)

    def test_two_pos_queued_from_the_same_cached_page_do_not_collapse_into_one(self):
        """Both forms were rendered with the same server-made submission_key.
        The ledger id replaces it, so they are two orders, not one."""
        self.client.force_login(self.manager)
        fields = {
            'submission_key': 'same-cached-token',
            'order_date': dt.date.today().isoformat(),
            'supplier': str(self.supplier.pk),
            'product_id': [str(self.product.pk)], 'quantity_ordered': ['5'],
            'funding_source': 'Bank Transfer',
        }
        a, _ = sync_post(self.client, self.manager, 'procurement.create_po', fields)
        b, _ = sync_post(self.client, self.manager, 'procurement.create_po', fields)
        self.assertEqual(a.json()['status'], 'applied', a.json())
        self.assertEqual(b.json()['status'], 'applied', b.json())
        self.assertEqual(Procurement.objects.count(), 2)



class ShellWiringTests(_SalesFixture, TestCase):
    """The pieces the browser needs actually reach it."""

    def setUp(self):
        self.build()

    def test_service_worker_renders_with_the_app_version(self):
        from core.version import APP_VERSION

        resp = self.client.get('/sw.js')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f"VERSION = '{APP_VERSION}'", body)
        self.assertNotIn('{{', body)
        self.assertNotIn('{%', body)
        # Writes are never the worker's business.
        self.assertIn("request.method !== 'GET'", body)

    def test_internal_shell_carries_identity_and_the_queue_script(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('inventory_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="gvOfflineConfig"')
        self.assertContains(resp, f'"userId": "{self.staff.pk}"')
        self.assertContains(resp, 'js/offline-sync')
        self.assertContains(resp, reverse('offline_ping'))
        self.assertContains(resp, reverse('offline_sync_apply'))

    def test_pos_page_asks_the_queue_to_adjust_its_stock_figures(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('transaction_add'))
        self.assertContains(resp, 'GerlynOffline.adjustStockMap')

    def test_queue_script_is_a_real_static_file(self):
        from django.contrib.staticfiles import finders

        self.assertIsNotNone(finders.find('js/offline-sync.js'))

    def test_storefront_does_not_load_the_internal_queue(self):
        resp = self.client.get(reverse('orders:catalog'))
        self.assertNotContains(resp, 'offline-sync')


class PaymentProofUploadTests(_SalesFixture, TestCase):
    """A GCash sale queued offline carries its screenshot as a real file part.
    The bytes sit in IndexedDB until sync, so the multipart path has to work."""

    def setUp(self):
        import shutil
        import tempfile

        from django.test import override_settings

        self.build()
        self.media = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, True)
        self.client.force_login(self.staff)

    @staticmethod
    def _png():
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buf = io.BytesIO()
        Image.new('RGB', (2, 2), color='white').save(buf, format='PNG')
        return SimpleUploadedFile('proof.png', buf.getvalue(), content_type='image/png')

    def _fields(self, **extra):
        fields = {
            'mode_of_payment': 'GCash', 'reference_number': '1234567890123',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
        }
        fields.update(extra)
        return fields

    def test_gcash_sale_with_its_screenshot_is_applied_and_stays_pending_for_verification(self):
        from billing.models import BillingRecord

        resp, _ = sync_post(
            self.client, self.staff, 'sale.walkin',
            self._fields(proof_of_payment=self._png()))
        data = resp.json()
        self.assertEqual(data['status'], 'applied', data)
        txn = SalesTransaction.objects.get()
        # Money that did not cross the counter is NOT settled by syncing: it
        # stays Pending until a second person verifies it, exactly as online.
        self.assertEqual(txn.order_status, SalesTransaction.OrderStatus.PENDING)
        billing = BillingRecord.objects.get(transaction=txn)
        self.assertEqual(billing.status, 'Pending')
        self.assertEqual(billing.reference_number, '1234567890123')
        self.assertTrue(billing.proof_of_payment)

    def test_gcash_sale_missing_its_screenshot_is_rejected_with_the_reason(self):
        resp, _ = sync_post(self.client, self.staff, 'sale.walkin', self._fields())
        data = resp.json()
        self.assertEqual(data['status'], 'rejected', data)
        self.assertIn('proof', data['message'].lower())
        self.assertFalse(SalesTransaction.objects.exists())

    def test_a_file_that_is_not_an_image_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        fake = SimpleUploadedFile('proof.png', b'<html>not an image</html>', content_type='image/png')
        resp, _ = sync_post(
            self.client, self.staff, 'sale.walkin', self._fields(proof_of_payment=fake))
        self.assertEqual(resp.json()['status'], 'rejected')
        self.assertFalse(SalesTransaction.objects.exists())


class DataEntrySyncTests(_SalesFixture, TestCase):
    """Creating records offline: products, categories, suppliers, write-offs.

    Every one replays the real view, so the online rules (unique names, valid
    prices, Owner-only suppliers) apply unchanged. Editing is not queueable.
    """

    def setUp(self):
        self.build()
        self.manager = User.objects.create_user(
            username='de-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        self.manager.manager_profile.branch = self.branch
        self.manager.manager_profile.save(update_fields=['branch'])
        # A new Branch Manager has no Add right on Products until the Owner
        # grants it, online or off. Grant it here so the tests exercise the
        # creation rules, and test the missing grant separately.
        actions = dict(self.manager.module_actions or {})
        actions[PRODUCTS] = [ADD]
        self.manager.module_actions = actions
        self.manager.save(update_fields=['module_actions'])

    def _apply(self, user, op_type, fields, **kw):
        self.client.force_login(user)
        resp, op_id = sync_post(self.client, user, op_type, fields, **kw)
        return resp, resp.json(), op_id

    # -- product -------------------------------------------------------------
    def _product_fields(self, **over):
        fields = {
            'product_name': 'Offline Noodles', 'unit_of_measure': 'Pack',
            'unit_price': '12.50', 'barcode': 'OFF-BC-1', 'branches': [str(self.branch.pk)],
        }
        fields.update(over)
        return fields

    def test_product_is_created_through_the_real_view(self):
        _resp, data, _ = self._apply(
            self.manager, 'inventory.product_add', self._product_fields())
        self.assertEqual(data['status'], 'applied', data)
        product = Product.objects.get(product_name='Offline Noodles')
        self.assertEqual(product.unit_price, Decimal('12.50'))
        self.assertTrue(Inventory.objects.filter(product=product, branch=self.branch).exists())
        self.assertEqual(data['result']['label'], 'Offline Noodles')

    def test_product_with_a_taken_barcode_is_rejected_and_not_created(self):
        Product.objects.filter(pk=self.product.pk).update(barcode='OFF-BC-1')
        _resp, data, _ = self._apply(
            self.manager, 'inventory.product_add', self._product_fields())
        self.assertEqual(data['status'], 'rejected', data)
        self.assertIn('already assigned', data['message'])
        self.assertFalse(Product.objects.filter(product_name='Offline Noodles').exists())

    def test_product_with_a_bad_price_is_rejected_with_the_reason(self):
        _resp, data, _ = self._apply(
            self.manager, 'inventory.product_add', self._product_fields(unit_price='abc'))
        self.assertEqual(data['status'], 'rejected', data)
        self.assertTrue(data['message'])
        self.assertFalse(Product.objects.filter(product_name='Offline Noodles').exists())

    def test_manager_without_the_add_grant_is_refused_like_online(self):
        self.manager.module_actions = {PRODUCTS: []}
        self.manager.save(update_fields=['module_actions'])
        _resp, data, _ = self._apply(
            self.manager, 'inventory.product_add', self._product_fields())
        self.assertEqual(data['status'], 'rejected', data)
        self.assertIn('Add permission', data['message'])
        self.assertFalse(Product.objects.filter(product_name='Offline Noodles').exists())

    def test_staff_cannot_queue_a_new_product(self):
        resp, _data, _ = self._apply(
            self.staff, 'inventory.product_add', self._product_fields())
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Product.objects.filter(product_name='Offline Noodles').exists())

    def test_resending_a_product_never_creates_it_twice(self):
        op_id = uuid.uuid4().hex
        self.client.force_login(self.manager)
        sync_post(self.client, self.manager, 'inventory.product_add',
                  self._product_fields(), op_id=op_id)
        resp, _ = sync_post(self.client, self.manager, 'inventory.product_add',
                            self._product_fields(), op_id=op_id)
        self.assertTrue(resp.json().get('duplicate'))
        self.assertEqual(Product.objects.filter(product_name='Offline Noodles').count(), 1)

    # -- category ------------------------------------------------------------
    def test_category_is_created_and_a_duplicate_is_rejected(self):
        _r, data, _ = self._apply(
            self.manager, 'inventory.category_add', {'category_name': 'Snacks'})
        self.assertEqual(data['status'], 'applied', data)
        _r, again, _ = self._apply(
            self.manager, 'inventory.category_add', {'category_name': 'snacks'})
        self.assertEqual(again['status'], 'rejected', again)
        self.assertIn('already exists', again['message'])

    # -- suppliers -----------------------------------------------------------
    def test_owner_can_queue_a_supplier_and_it_gets_no_usable_password(self):
        _r, data, _ = self._apply(self.owner, 'procurement.supplier_add', {
            'supplier_name': 'Baliwag Wholesale', 'username': 'baliwag-ws',
            'email': 'ws@example.com',
        })
        self.assertEqual(data['status'], 'applied', data)
        supplier = User.objects.get(username='baliwag-ws')
        self.assertEqual(supplier.role, User.Role.SUPPLIER)
        self.assertEqual(supplier.supplier_profile.supplier_name, 'Baliwag Wholesale')
        self.assertFalse(supplier.check_password('pw'))

    def test_supplier_with_a_taken_username_is_rejected(self):
        _r, data, _ = self._apply(self.owner, 'procurement.supplier_add', {
            'supplier_name': 'Clash', 'username': self.staff.username,
        })
        self.assertEqual(data['status'], 'rejected', data)
        self.assertIn('already taken', data['message'])

    def test_manager_cannot_queue_a_supplier(self):
        resp, _d, _ = self._apply(self.manager, 'procurement.supplier_add', {
            'supplier_name': 'Nope', 'username': 'nope-sup',
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(User.objects.filter(username='nope-sup').exists())

    def test_supplier_category_is_created(self):
        _r, data, _ = self._apply(
            self.owner, 'procurement.supplier_category_add', {'name': 'Beverages'})
        self.assertEqual(data['status'], 'applied', data)
        _r, again, _ = self._apply(
            self.owner, 'procurement.supplier_category_add', {'name': 'Beverages'})
        self.assertEqual(again['status'], 'rejected', again)

    def test_product_can_be_linked_to_a_supplier(self):
        supplier = User.objects.create_user(
            username='link-sup', password='pw', role=User.Role.SUPPLIER)
        _r, data, _ = self._apply(
            self.owner, 'procurement.supplier_product_add',
            {'product': str(self.product.pk), 'lead_time_days': '3'},
            target_pk=supplier.pk)
        self.assertEqual(data['status'], 'applied', data)
        _r, again, _ = self._apply(
            self.owner, 'procurement.supplier_product_add',
            {'product': str(self.product.pk)}, target_pk=supplier.pk)
        # Already linked: the view answers kindly but this change did nothing.
        self.assertEqual(again['status'], 'conflict', again)

    # -- write-off -----------------------------------------------------------
    def test_write_off_request_is_queued_and_moves_no_stock(self):
        Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=10, received_date=dt.date.today())
        _r, data, _ = self._apply(self.manager, 'inventory.write_off', {
            'purpose': StockTransfer.Purpose.OTHER,
            'product_id': [str(self.product.pk)], 'quantity_requested': ['2'],
            'notes': 'Dented cans',
        })
        self.assertEqual(data['status'], 'applied', data)
        transfer = StockTransfer.objects.get()
        self.assertEqual(data['result']['label'], transfer.slip_number)
        self.assertEqual(self.stock(), 10, 'nothing leaves the floor until it is completed online')

    def test_write_off_for_more_than_exists_is_rejected(self):
        _r, data, _ = self._apply(self.manager, 'inventory.write_off', {
            'purpose': StockTransfer.Purpose.OTHER,
            'product_id': [str(self.product.pk)], 'quantity_requested': ['999'],
        })
        self.assertEqual(data['status'], 'rejected', data)
        self.assertFalse(StockTransfer.objects.exists())
