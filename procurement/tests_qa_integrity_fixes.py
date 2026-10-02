"""Regression coverage for the Joshua procurement QA findings."""

import datetime as dt
import json
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from core.module_permissions import DELIVERIES, EDIT, PROCUREMENT
from inventory.models import Product, StockMovement, Warehouse, WarehouseStock
from procurement.models import (
    DeliveryReceipt,
    DeliveryReceiptItem,
    Procurement,
    ProcurementItem,
    SupplierProduct,
    SupplierReturn,
    SupplierReturnItem,
)
from procurement.services import (
    DeliveryError,
    create_supplier_return,
    record_delivery,
)
from procurement.tests import ProcurementAuthorizationBase
from reports.services import procurement_cost_total, procurement_return_totals


class DeliveryIntegrityRegressionTests(ProcurementAuthorizationBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.warehouse, _ = Warehouse.objects.get_or_create(
            branch=cls.home_branch, defaults={'name': 'QA Receiving Warehouse'},
        )

    def setUp(self):
        super().setUp()
        self.po = self._po(
            status=Procurement.Status.INCOMING,
            delivery_date=timezone.localdate(),
        )
        self.item = ProcurementItem.objects.create(
            procurement=self.po,
            product=self.product,
            quantity_ordered=10,
            unit_cost_snapshot=Decimal('12.50'),
        )

    def test_duplicate_receipt_token_does_not_duplicate_stock(self):
        first, _, duplicate_first = record_delivery(
            self.po, self.manager,
            lines={self.item.pk: (4, None)},
            idempotency_key='qa-receipt-once',
        )
        second, _, duplicate_second = record_delivery(
            self.po, self.manager,
            lines={self.item.pk: (4, None)},
            idempotency_key='qa-receipt-once',
        )

        self.item.refresh_from_db()
        stock = WarehouseStock.objects.get(
            warehouse=self.warehouse, product=self.product,
        )
        self.assertFalse(duplicate_first)
        self.assertTrue(duplicate_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.item.quantity_received, 4)
        self.assertEqual(stock.quantity_on_hand, 4)
        self.assertEqual(DeliveryReceipt.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(reference_id=self.po.pk).count(), 1)

    def test_rejected_delivery_requires_reason_and_never_adds_inventory(self):
        with self.assertRaisesMessage(DeliveryError, 'Enter a reason'):
            record_delivery(
                self.po, self.manager, lines={},
                idempotency_key='qa-rejected-missing-reason',
            )

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('api-procurement-receive', args=[self.po.pk]),
            data=json.dumps({
                'outcome': DeliveryReceipt.Outcome.REJECTED,
                'submissionKey': 'qa-rejected-with-reason',
                'rejectionReason': 'Wrong products were unloaded.',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        receipt = DeliveryReceipt.objects.get(idempotency_key='qa-rejected-with-reason')

        self.po.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(receipt.outcome, DeliveryReceipt.Outcome.REJECTED)
        self.assertEqual(receipt.rejection_reason, 'Wrong products were unloaded.')
        self.assertEqual(self.po.status, Procurement.Status.INCOMING)
        self.assertEqual(self.item.quantity_received, 0)
        self.assertFalse(WarehouseStock.objects.filter(product=self.product).exists())
        self.assertFalse(DeliveryReceiptItem.objects.exists())
        self.assertFalse(StockMovement.objects.filter(reference_id=self.po.pk).exists())
        detail = self.client.get(reverse('procurement_detail', args=[self.po.pk]))
        self.assertContains(detail, 'Wrong products were unloaded.')
        self.assertContains(detail, 'Failed / rejected')

    def test_wrong_po_line_and_over_receipt_are_rejected_atomically(self):
        foreign_po = self._po(status=Procurement.Status.INCOMING)
        foreign_item = ProcurementItem.objects.create(
            procurement=foreign_po, product=self.product, quantity_ordered=1,
        )
        with self.assertRaisesMessage(DeliveryError, 'not on this purchase order'):
            record_delivery(
                self.po, self.manager,
                lines={foreign_item.pk: (1, None)},
                idempotency_key='qa-wrong-line',
            )
        with self.assertRaisesMessage(DeliveryError, 'cannot exceed'):
            record_delivery(
                self.po, self.manager,
                lines={self.item.pk: (11, None)},
                idempotency_key='qa-over-receipt',
            )
        self.assertFalse(DeliveryReceipt.objects.exists())
        self.assertFalse(WarehouseStock.objects.filter(product=self.product).exists())

    def test_return_is_allocated_to_the_original_receipt_line(self):
        receipt, _, _ = record_delivery(
            self.po, self.manager,
            lines={self.item.pk: (10, None)},
            idempotency_key='qa-return-source',
        )
        supplier_return = create_supplier_return(
            self.po, self.manager,
            reason=SupplierReturn.Reason.DAMAGED,
            notes='Crushed cartons', quantities={self.item.pk: 3},
        )

        return_line = supplier_return.items.get()
        self.assertEqual(return_line.source_receipt_item.receipt, receipt)
        self.assertEqual(return_line.source_receipt_item.procurement_item, self.item)
        self.assertEqual(return_line.source_receipt_item.product, self.product)
        self.assertEqual(supplier_return.supplier, self.supplier)
        self.assertEqual(supplier_return.branch, self.home_branch)


class ProcurementSecurityRegressionTests(ProcurementAuthorizationBase):
    def test_manager_supplier_detail_does_not_leak_foreign_branch_orders(self):
        own = self._po(branch=self.home_branch, status=Procurement.Status.DRAFT)
        foreign = self._po(branch=self.other_branch, status=Procurement.Status.DRAFT)
        self.client.force_login(self.manager)

        response = self.client.get(reverse('supplier_detail', args=[self.supplier.pk]))

        self.assertEqual(response.status_code, 200)
        ids = list(response.context['recent_orders'].values_list('pk', flat=True))
        self.assertIn(own.pk, ids)
        self.assertNotIn(foreign.pk, ids)

    def test_manager_receiving_uses_delivery_edit_permission(self):
        Warehouse.objects.get_or_create(
            branch=self.home_branch, defaults={'name': 'Permission Warehouse'},
        )
        po = self._po(status=Procurement.Status.INCOMING)
        item = ProcurementItem.objects.create(
            procurement=po, product=self.product, quantity_ordered=2,
        )
        self.manager.allowed_modules = [PROCUREMENT, DELIVERIES]
        self.manager.module_actions = {
            PROCUREMENT: [EDIT],
            DELIVERIES: [],
        }
        self.manager.save(update_fields=['allowed_modules', 'module_actions'])
        self.client.force_login(self.manager)
        payload = {
            'outcome': DeliveryReceipt.Outcome.ACCEPTED,
            'submissionKey': 'qa-permission-receive',
            'items': [{'itemId': item.pk, 'quantityReceived': 2}],
        }

        denied = self.client.post(
            reverse('api-procurement-receive', args=[po.pk]),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(denied.status_code, 403)

        self.manager.module_actions[DELIVERIES] = [EDIT]
        self.manager.save(update_fields=['module_actions'])
        allowed = self.client.post(
            reverse('api-procurement-receive', args=[po.pk]),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_manager_cannot_receive_another_branch_po(self):
        Warehouse.objects.get_or_create(
            branch=self.other_branch, defaults={'name': 'Foreign Warehouse'},
        )
        po = self._po(branch=self.other_branch, status=Procurement.Status.INCOMING)
        item = ProcurementItem.objects.create(
            procurement=po, product=self.product, quantity_ordered=1,
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('api-procurement-receive', args=[po.pk]),
            data=json.dumps({
                'outcome': DeliveryReceipt.Outcome.ACCEPTED,
                'submissionKey': 'qa-foreign-receive',
                'items': [{'itemId': item.pk, 'quantityReceived': 1}],
            }),
            content_type='application/json',
        )
        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(DeliveryReceipt.objects.exists())


class SupplierIdentityAndLifecycleTests(ProcurementAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def _supplier_payload(self, **overrides):
        payload = {
            'supplier_name': 'Independent Trading',
            'username': 'independent-supplier',
            'email': 'independent@example.test',
            'business_registration_id': 'TIN-0002',
            'contact_person': 'Inez',
            'contact_number': '09171234567',
            'address': 'Baliuag',
            'branch': str(self.home_branch.pk),
        }
        payload.update(overrides)
        return payload

    def test_name_match_warns_but_unique_identity_fields_allow_creation(self):
        self.supplier.supplier_profile.supplier_name = 'Independent Trading'
        self.supplier.supplier_profile.save(update_fields=['supplier_name'])

        response = self.client.post(reverse('supplier_add'), self._supplier_payload())

        self.assertEqual(response.status_code, 302)
        created = User.objects.get(username='independent-supplier')
        self.assertEqual(created.email, 'independent@example.test')
        self.assertTrue(any(
            'similar name or contact' in message.message
            for message in get_messages(response.wsgi_request)
        ))

    def test_supplier_email_and_registration_are_unique(self):
        first = self.client.post(reverse('supplier_add'), self._supplier_payload())
        self.assertEqual(first.status_code, 302)

        duplicate_email = self.client.post(reverse('supplier_add'), self._supplier_payload(
            supplier_name='Other Name', username='email-duplicate',
            business_registration_id='TIN-0003',
        ))
        self.assertEqual(duplicate_email.status_code, 200)
        self.assertContains(duplicate_email, 'already used by another supplier')

        duplicate_registration = self.client.post(reverse('supplier_add'), self._supplier_payload(
            supplier_name='Third Name', username='registration-duplicate',
            email='third@example.test',
        ))
        self.assertEqual(duplicate_registration.status_code, 200)
        self.assertContains(duplicate_registration, 'registration identifier already belongs')

    def test_deactivation_preserves_supplier_history_and_catalog(self):
        po = self._po(status=Procurement.Status.RECEIVED)
        item = ProcurementItem.objects.create(
            procurement=po, product=self.product, quantity_ordered=1,
            quantity_received=1,
        )
        supplier_return = SupplierReturn.objects.create(
            procurement=po, supplier=self.supplier, branch=self.home_branch,
            reason=SupplierReturn.Reason.DAMAGED, requested_by=self.owner,
        )
        SupplierReturnItem.objects.create(
            supplier_return=supplier_return, procurement_item=item,
            product=self.product, quantity_returned=1,
        )
        link_pk = SupplierProduct.objects.get(
            supplier=self.supplier, product=self.product,
        ).pk

        response = self.client.post(reverse('supplier_delete', args=[self.supplier.pk]))

        self.assertEqual(response.status_code, 302)
        self.supplier.refresh_from_db()
        po.refresh_from_db()
        supplier_return.refresh_from_db()
        self.assertFalse(self.supplier.is_active)
        self.assertEqual(po.supplier_id, self.supplier.pk)
        self.assertEqual(supplier_return.supplier_id, self.supplier.pk)
        self.assertTrue(SupplierProduct.objects.filter(pk=link_pk).exists())


class PurchaseOrderIdempotencyAndReportingTests(ProcurementAuthorizationBase):
    def test_web_submission_key_creates_only_one_purchase_order(self):
        self.client.force_login(self.manager)
        payload = {
            'branch': str(self.home_branch.pk),
            'supplier': str(self.supplier.pk),
            'order_date': timezone.localdate().isoformat(),
            'funding_source': Procurement.FundingSource.CASH,
            'product_id': [str(self.product.pk)],
            'quantity_ordered': ['2'],
            'submission_key': 'qa-po-submit-once',
        }

        first = self.client.post(reverse('procurement_add'), payload)
        second = self.client.post(reverse('procurement_add'), payload)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            Procurement.objects.filter(submission_key='qa-po-submit-once').count(), 1,
        )

    def test_api_submission_key_returns_the_same_purchase_order(self):
        self.client.force_login(self.manager)
        payload = {
            'branchId': self.home_branch.pk,
            'supplierId': self.supplier.pk,
            'orderDate': timezone.localdate().isoformat(),
            'submissionKey': 'qa-api-po-submit-once',
            'fundingSource': Procurement.FundingSource.CASH,
            'items': [{'productId': self.product.pk, 'quantityOrdered': 2}],
        }

        first = self.client.post(
            reverse('api-procurement-list'),
            data=json.dumps(payload), content_type='application/json',
        )
        second = self.client.post(
            reverse('api-procurement-list'),
            data=json.dumps(payload), content_type='application/json',
        )

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(
            Procurement.objects.filter(submission_key='qa-api-po-submit-once').count(), 1,
        )

    def test_gross_returns_credits_and_net_are_separate(self):
        po = self._po(status=Procurement.Status.RECEIVED)
        po.order_date = timezone.localdate()
        po.total_cost = Decimal('100.00')
        po.save(update_fields=['order_date', 'total_cost'])
        item = ProcurementItem.objects.create(
            procurement=po, product=self.product, quantity_ordered=10,
            quantity_received=10, unit_cost_snapshot=Decimal('10.00'),
        )
        shipped = SupplierReturn.objects.create(
            procurement=po, supplier=self.supplier, branch=self.home_branch,
            reason=SupplierReturn.Reason.DAMAGED,
            status=SupplierReturn.Status.SHIPPED, requested_by=self.manager,
        )
        SupplierReturnItem.objects.create(
            supplier_return=shipped, procurement_item=item,
            product=self.product, quantity_returned=2,
        )
        credited = SupplierReturn.objects.create(
            procurement=po, supplier=self.supplier, branch=self.home_branch,
            reason=SupplierReturn.Reason.QUALITY,
            status=SupplierReturn.Status.SETTLED,
            resolution=SupplierReturn.Resolution.CREDIT,
            credit_amount=Decimal('15.00'), requested_by=self.manager,
        )
        SupplierReturnItem.objects.create(
            supplier_return=credited, procurement_item=item,
            product=self.product, quantity_returned=1,
        )

        gross = procurement_cost_total(Procurement.objects.filter(pk=po.pk))
        returns = procurement_return_totals(Procurement.objects.filter(pk=po.pk))

        self.assertEqual(gross, Decimal('100.00'))
        self.assertEqual(returns['returned_goods'], Decimal('30.00'))
        self.assertEqual(returns['open_return_adjustment'], Decimal('20.00'))
        self.assertEqual(returns['credits'], Decimal('15.00'))
        self.assertEqual(gross - returns['net_adjustment'], Decimal('65.00'))
        po.refresh_from_db()
        self.assertEqual(po.total_cost, Decimal('100.00'))

        self.client.force_login(self.owner)
        web = self.client.get(reverse('procurement_report')).context
        api = self.client.get(reverse('api-reports-dashboard')).json()
        self.assertEqual(web['gross_procurement'], Decimal('100.00'))
        self.assertEqual(web['supplier_return_value'], Decimal('30.00'))
        self.assertEqual(web['supplier_credit_value'], Decimal('15.00'))
        self.assertEqual(web['net_procurement'], Decimal('65.00'))
        self.assertEqual(Decimal(str(api['grossProcurement'])), Decimal('100.00'))
        self.assertEqual(Decimal(str(api['supplierReturnValue'])), Decimal('30.00'))
        self.assertEqual(Decimal(str(api['supplierCreditValue'])), Decimal('15.00'))
        self.assertEqual(Decimal(str(api['netProcurement'])), Decimal('65.00'))
