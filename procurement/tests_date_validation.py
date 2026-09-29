from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from branches.models import Branch
from core.date_validation import TODAY_OR_FUTURE_MESSAGE
from inventory.models import Product
from procurement.models import Procurement, ProcurementItem, SupplierProduct
from procurement.services import apply_agreed_terms


@override_settings(STORAGES={
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
})
class ProcurementDateValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='date-owner', password='pw', role=User.Role.OWNER
        )
        cls.supplier = User.objects.create_user(
            username='date-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.branch = Branch.objects.create(
            branch_name='Date Policy Branch', location='Baliuag', owner=cls.owner
        )
        cls.product = Product.objects.create(
            sku_code='DATE-001', product_name='Date Test Product', unit_price=50
        )
        SupplierProduct.objects.create(
            supplier=cls.supplier,
            product=cls.product,
            current_cost_price=25,
            status=SupplierProduct.Status.APPROVED,
        )

    def setUp(self):
        self.today = timezone.localdate()
        self.web = Client()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def make_po(self, status=Procurement.Status.APPROVED, **extra):
        procurement = Procurement.objects.create(
            branch=self.branch,
            supplier=self.supplier,
            owner=self.owner,
            order_date=self.today,
            status=status,
            funding_source=Procurement.FundingSource.CASH,
            **extra,
        )
        item = ProcurementItem.objects.create(
            procurement=procurement,
            product=self.product,
            quantity_ordered=2,
        )
        return procurement, item

    def proposal_payload(self, item, delivery_date):
        return {
            'action': 'propose',
            'delivery_date': delivery_date.isoformat(),
            f'unit_cost_{item.pk}': '25.00',
        }

    def api_payload(self, expected_delivery=None, order_date=None):
        payload = {
            'branchId': self.branch.pk,
            'supplierId': self.supplier.pk,
            'orderDate': (order_date or self.today).isoformat(),
            'fundingSource': Procurement.FundingSource.CASH,
            'items': [{
                'productId': self.product.pk,
                'quantityOrdered': 1,
            }],
        }
        if expected_delivery is not None:
            payload['expectedDelivery'] = expected_delivery.isoformat()
        return payload

    def html_create_payload(self, order_date):
        return {
            'branch': str(self.branch.pk),
            'supplier': str(self.supplier.pk),
            'order_date': order_date.isoformat(),
            'funding_source': Procurement.FundingSource.CASH,
            'product_id': [str(self.product.pk)],
            'quantity_ordered': ['1'],
        }

    def test_yesterday_rejected_by_template_and_api_with_same_message(self):
        yesterday = self.today - timedelta(days=1)
        procurement, item = self.make_po()
        self.web.force_login(self.supplier)

        html_response = self.web.post(
            reverse('procurement_supplier_respond', args=[procurement.pk]),
            self.proposal_payload(item, yesterday),
            follow=True,
        )
        api_response = self.api.post(
            reverse('api-procurement-list'),
            self.api_payload(yesterday),
            format='json',
        )

        procurement.refresh_from_db()
        self.assertEqual(procurement.status, Procurement.Status.APPROVED)
        self.assertContains(html_response, TODAY_OR_FUTURE_MESSAGE)
        self.assertEqual(api_response.status_code, 400)
        self.assertEqual(
            str(api_response.data['expectedDelivery'][0]),
            TODAY_OR_FUTURE_MESSAGE,
        )

    def test_today_and_future_are_accepted_by_template_and_api(self):
        self.web.force_login(self.supplier)
        for offset in (0, 30):
            with self.subTest(offset=offset):
                candidate = self.today + timedelta(days=offset)
                procurement, item = self.make_po()
                response = self.web.post(
                    reverse('procurement_supplier_respond', args=[procurement.pk]),
                    self.proposal_payload(item, candidate),
                )
                procurement.refresh_from_db()
                self.assertEqual(response.status_code, 302)
                self.assertEqual(procurement.proposed_delivery_date, candidate)

                api_response = self.api.post(
                    reverse('api-procurement-list'),
                    self.api_payload(candidate),
                    format='json',
                )
                self.assertEqual(api_response.status_code, 201, api_response.data)

    def test_historical_order_dates_remain_allowed(self):
        historical = self.today - timedelta(days=365)
        self.web.force_login(self.owner)

        html_response = self.web.post(
            reverse('procurement_add'), self.html_create_payload(historical)
        )
        api_response = self.api.post(
            reverse('api-procurement-list'),
            self.api_payload(order_date=historical),
            format='json',
        )

        self.assertEqual(html_response.status_code, 302)
        self.assertEqual(api_response.status_code, 201, api_response.data)

    def test_actual_receipt_date_may_be_historical(self):
        historical = self.today - timedelta(days=10)
        procurement, item = self.make_po(
            status=Procurement.Status.INCOMING,
            delivery_date=self.today - timedelta(days=2),
        )

        response = self.api.patch(
            reverse('api-procurement-status', args=[procurement.pk]),
            {
                'status': Procurement.Status.RECEIVED,
                'actualReceiptDate': historical.isoformat(),
                'items': [{
                    'itemId': item.pk,
                    'quantityReceived': item.quantity_ordered,
                }],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        procurement.refresh_from_db()
        self.assertEqual(procurement.actual_receipt_date, historical)
        self.assertEqual(procurement.delivery_date, self.today - timedelta(days=2))

    def test_agreed_terms_are_revalidated_at_service_transition(self):
        yesterday = self.today - timedelta(days=1)
        procurement, item = self.make_po(
            status=Procurement.Status.TERMS_AGREED,
            proposed_delivery_date=yesterday,
        )
        item.proposed_unit_cost = 25
        item.save(update_fields=['proposed_unit_cost'])

        with self.assertRaisesMessage(ValidationError, TODAY_OR_FUTURE_MESSAGE):
            apply_agreed_terms(procurement)

        procurement.refresh_from_db()
        item.refresh_from_db()
        self.assertIsNone(procurement.delivery_date)
        self.assertIsNone(item.unit_cost_snapshot)

    def test_future_action_inputs_publish_local_today_as_minimum(self):
        procurement, _ = self.make_po()
        self.web.force_login(self.supplier)

        detail = self.web.get(
            reverse('procurement_detail', args=[procurement.pk])
        )
        supplier_orders = self.web.get(reverse('supplier_purchase_orders'))

        expected = f'min="{self.today.isoformat()}"'
        self.assertContains(detail, expected)
        self.assertContains(supplier_orders, expected)
