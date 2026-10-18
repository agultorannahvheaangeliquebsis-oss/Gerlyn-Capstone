import datetime as dt
import json
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffProfile, User
from branches.models import Branch
from inventory.models import Product, StockMovement, Warehouse, WarehouseStock

from .models import Procurement, ProcurementItem


class OptionalSupplierParticipationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='optional-owner', password='pw', role=User.Role.OWNER,
        )
        cls.supplier = User.objects.create_user(
            username='optional-supplier', password='pw', role=User.Role.SUPPLIER,
        )
        cls.branch = Branch.objects.create(
            branch_name='Optional Supplier Branch', location='Baliuag',
            owner=cls.owner,
        )
        cls.staff = User.objects.create_user(
            username='optional-staff', password='pw', role=User.Role.STAFF,
        )
        StaffProfile.objects.update_or_create(
            user=cls.staff,
            defaults={
                'branch': cls.branch, 'staff_name': 'Receiver',
                'contact_number': '09000000000',
            },
        )
        cls.product = Product.objects.create(
            sku_code='OPTIONAL-PO-1', product_name='Optional Portal Product',
            unit_price=Decimal('75.00'),
        )
        cls.warehouse, _ = Warehouse.objects.get_or_create(
            branch=cls.branch, defaults={'name': 'Optional Supplier Warehouse'},
        )

    def setUp(self):
        self.client = Client()
        self.po = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            created_by=self.owner, order_date=timezone.localdate(),
            status=Procurement.Status.APPROVED,
            authorized_at=timezone.now(), funding_source='Cash on Hand',
        )
        self.item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product, quantity_ordered=10,
        )

    def communication_payload(self):
        return {
            'communication_method': Procurement.CommunicationMethod.MESSAGE,
            'communication_note': 'Confirmed through Viber.',
            'delivery_date': (timezone.localdate() + dt.timedelta(days=5)).isoformat(),
            f'unit_cost_{self.item.pk}': '50.00',
        }

    def mark_communicated(self, user=None):
        self.client.force_login(user or self.owner)
        return self.client.post(
            reverse('procurement_mark_communicated', args=[self.po.pk]),
            self.communication_payload(),
        )

    def test_internal_user_can_order_without_supplier_login(self):
        response = self.mark_communicated()

        self.assertRedirects(
            response, reverse('delivery_detail', args=[self.po.pk]),
            fetch_redirect_response=False,
        )
        self.po.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.ORDERED)
        self.assertEqual(self.po.communicated_by, self.owner)
        self.assertEqual(self.po.communication_method, 'Message')
        self.assertEqual(self.po.total_cost, Decimal('500.00'))
        self.assertEqual(self.item.unit_cost_snapshot, Decimal('50.00'))

    def test_rendered_form_posts_cost_keys_the_view_reads_for_four_digit_pks(self):
        """USE_THOUSAND_SEPARATOR localises `name="unit_cost_{{ item.pk }}"` to
        unit_cost_1,502, which request.POST.get('unit_cost_1502') never finds
        ("Unit cost for X is required." with the value visibly typed in).
        The other tests post the key directly with a small pk, so only a
        round trip through the rendered form catches it."""
        import re

        big = ProcurementItem.objects.create(
            pk=1502, procurement=self.po, product=self.product,
            quantity_ordered=20,
        )
        self.item.delete()
        self.client.force_login(self.owner)
        html = self.client.get(
            reverse('delivery_detail', args=[self.po.pk]),
        ).content.decode()
        names = re.findall(r'name="(unit_cost_[^"]*)"', html)
        self.assertEqual(names, ['unit_cost_1502'])

        payload = self.communication_payload()
        payload.pop(f'unit_cost_{self.item.pk}')
        payload[names[0]] = '30'
        response = self.client.post(
            reverse('procurement_mark_communicated', args=[self.po.pk]), payload,
        )

        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()
        big.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.ORDERED)
        self.assertEqual(big.unit_cost_snapshot, Decimal('30.00'))
        self.assertEqual(self.po.total_cost, Decimal('600.00'))

    def test_supplier_portal_form_posts_cost_keys_the_view_reads_for_four_digit_pks(self):
        """Same localisation bug as the internal form, on the supplier's own
        propose-terms form (_supplier_po_detail.html)."""
        import re

        big = ProcurementItem.objects.create(
            pk=1502, procurement=self.po, product=self.product,
            quantity_ordered=20,
        )
        self.item.delete()
        self.client.force_login(self.supplier)
        html = self.client.get(reverse('supplier_purchase_orders')).content.decode()
        names = re.findall(r'name="(unit_cost_[^"]*)"', html)
        self.assertEqual(names, ['unit_cost_1502'])

        response = self.client.post(
            reverse('procurement_supplier_respond', args=[self.po.pk]),
            {
                'action': 'propose',
                'delivery_date': (timezone.localdate() + dt.timedelta(days=5)).isoformat(),
                names[0]: '30',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()
        big.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.PENDING_OWNER_REVIEW)
        self.assertEqual(big.proposed_unit_cost, Decimal('30.00'))

    def test_one_missing_cost_leaves_the_po_and_other_items_untouched(self):
        other_product = Product.objects.create(
            sku_code='OPTIONAL-PO-2', product_name='Second Portal Product',
            unit_price=Decimal('20.00'),
        )
        other = ProcurementItem.objects.create(
            procurement=self.po, product=other_product, quantity_ordered=5,
        )
        self.client.force_login(self.owner)
        payload = self.communication_payload()  # costs only the first item
        response = self.client.post(
            reverse('procurement_mark_communicated', args=[self.po.pk]), payload,
        )

        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()
        self.item.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.APPROVED)
        self.assertIsNone(self.item.unit_cost_snapshot)
        self.assertIsNone(other.unit_cost_snapshot)

    def test_staff_can_record_external_order_for_own_branch(self):
        response = self.mark_communicated(self.staff)

        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.ORDERED)
        self.assertEqual(self.po.communicated_by, self.staff)

    def test_supplier_can_acknowledge_but_acknowledgement_is_not_required(self):
        self.mark_communicated()
        self.client.force_login(self.supplier)
        response = self.client.post(
            reverse('procurement_supplier_confirm', args=[self.po.pk]),
        )

        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.INCOMING)
        self.assertIsNotNone(self.po.supplier_acknowledged_at)

    def test_supplier_can_acknowledge_an_approved_po_without_blocking_workflow(self):
        self.client.force_login(self.supplier)
        response = self.client.post(
            reverse('procurement_supplier_respond', args=[self.po.pk]),
            {'action': 'acknowledge'},
        )

        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.APPROVED)
        self.assertIsNotNone(self.po.supplier_acknowledged_at)

    def test_verified_receipt_from_communicated_po_updates_only_received_quantity(self):
        self.mark_communicated()
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('delivery_receive', args=[self.po.pk]),
            {
                'item_id': [str(self.item.pk)],
                'quantity_received': ['4'],
                'actual_receipt_date': timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.po.refresh_from_db()
        self.item.refresh_from_db()
        stock = WarehouseStock.objects.get(
            warehouse=self.warehouse, product=self.product,
        )
        self.assertEqual(self.po.status, Procurement.Status.PARTIALLY_RECEIVED)
        self.assertEqual(self.item.quantity_received, 4)
        self.assertEqual(stock.quantity_on_hand, 4)
        movement = StockMovement.objects.get(reference_id=self.po.pk)
        self.assertEqual(movement.quantity_change, 4)

    def test_api_records_external_order_terms(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('api-procurement-communicate', args=[self.po.pk]),
            data=json.dumps({
                'communicationMethod': 'Phone',
                'communicationNote': 'Called purchasing contact.',
                'deliveryDate': (
                    timezone.localdate() + dt.timedelta(days=4)
                ).isoformat(),
                'unitCosts': [{
                    'itemId': self.item.pk, 'unitCost': '51.25',
                }],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['status'], Procurement.Status.ORDERED)
        self.assertEqual(response.json()['total'], '512.50')

    def test_bare_status_update_cannot_bypass_external_terms(self):
        self.client.force_login(self.owner)
        response = self.client.patch(
            reverse('api-procurement-status', args=[self.po.pk]),
            data=json.dumps({'status': Procurement.Status.ORDERED}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.APPROVED)
