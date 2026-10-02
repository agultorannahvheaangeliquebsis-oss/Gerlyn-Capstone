"""Evaluator #4 — fewer dead ends and re-dos in Purchase Order receiving.

Every exit of delivery_receive used to land on procurement_detail, which the
module gate reserves for PROCUREMENT. Default Staff only hold DELIVERIES, so
their every receipt (and every count mistake) ended on a "no access" bounce.
A fixable count mistake also threw the receiver out of the Receive modal.
"""
import datetime as dt

from django.contrib.messages import get_messages
from django.urls import reverse

from accounts.models import StaffProfile, User
from core.module_permissions import DEFAULT_STAFF_MODULES, PROCUREMENT
from inventory.models import Warehouse, WarehouseStock
from procurement.models import Procurement, ProcurementItem
from procurement.tests import ProcurementAuthorizationBase


class ReceivingExitTests(ProcurementAuthorizationBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.warehouse, _ = Warehouse.objects.get_or_create(
            branch=cls.home_branch, defaults={'name': 'Friction Warehouse'})
        cls.staff = User.objects.create_user(
            username='rf-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=cls.staff,
            defaults={'branch': cls.home_branch, 'staff_name': 'Sam',
                      'contact_number': '0900'},
        )
        cls.staff.allowed_modules = list(DEFAULT_STAFF_MODULES)
        cls.staff.save(update_fields=['allowed_modules'])

    def setUp(self):
        super().setUp()
        self.po = self._po(
            status=Procurement.Status.INCOMING, delivery_date=dt.date(2026, 2, 10))
        self.item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product, quantity_ordered=10)

    def _receive(self, user, qty, **kw):
        self.client.force_login(user)
        return self.client.post(
            reverse('delivery_receive', args=[self.po.pk]),
            {'item_id': [str(self.item.pk)], 'quantity_received': [str(qty)],
             'actual_receipt_date': '2026-02-11'},
            **kw,
        )

    def test_precondition_default_staff_lack_procurement(self):
        self.assertNotIn(PROCUREMENT, DEFAULT_STAFF_MODULES)

    def test_staff_full_receipt_lands_on_received_tab_not_a_bounce(self):
        resp = self._receive(self.staff, 10)
        self.assertEqual(
            resp['Location'], f"{reverse('delivery_list')}?status=Received")
        page = self.client.get(resp['Location'])
        self.assertEqual(page.status_code, 200)
        text = ' '.join(str(m) for m in get_messages(page.wsgi_request))
        self.assertIn('received in full', text)
        self.assertNotIn('access', text.lower())

    def test_staff_partial_receipt_lands_on_partially_received_tab(self):
        resp = self._receive(self.staff, 4)
        self.assertEqual(
            resp['Location'],
            f"{reverse('delivery_list')}?status=Partially+Received")

    def test_owner_still_lands_on_the_po(self):
        resp = self._receive(self.owner, 10)
        self.assertRedirects(
            resp, reverse('procurement_detail', args=[self.po.pk]),
            fetch_redirect_response=False)

    def test_over_count_reopens_the_receive_modal_and_writes_nothing(self):
        resp = self._receive(self.manager, 99, follow=True)
        self.assertRedirects(
            resp, f"{reverse('delivery_list')}?receive={self.po.pk}")
        self.assertContains(resp, 'id="receive-modal"')
        self.assertContains(resp, 'cannot exceed the remaining quantity')
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, Procurement.Status.INCOMING)
        self.assertFalse(WarehouseStock.objects.filter(product=self.product).exists())

    def test_zero_count_reopens_the_receive_modal(self):
        resp = self._receive(self.staff, 0)
        self.assertEqual(resp['Location'], reverse('delivery_receive', args=[self.po.pk]))

    def test_staff_opening_an_already_received_po_is_not_bounced(self):
        self.po.status = Procurement.Status.RECEIVED
        self.po.save(update_fields=['status'])
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('delivery_receive', args=[self.po.pk]))
        self.assertEqual(
            resp['Location'], f"{reverse('delivery_list')}?status=Received")

    def test_rejected_count_reopens_the_modal_with_what_was_typed(self):
        self.client.force_login(self.manager)
        resp = self.client.post(
            reverse('delivery_receive', args=[self.po.pk]),
            {'item_id': [str(self.item.pk)], 'quantity_received': ['99'],
             'expiry_date': ['2027-01-31'], 'actual_receipt_date': '2026-02-11',
             'delivered_by': 'Juan'},
            follow=True,
        )
        self.assertContains(resp, 'cannot exceed the remaining quantity')
        self.assertContains(resp, 'name="quantity_received"')
        self.assertContains(resp, 'value="99"')
        self.assertNotContains(resp, 'value="10"')
        self.assertContains(resp, 'value="2027-01-31"')
        self.assertContains(resp, 'value="2026-02-11"')
        self.assertContains(resp, 'value="Juan"')

        # Shown once: opening Receive again starts from the defaults.
        again = self.client.get(f"{reverse('delivery_list')}?receive={self.po.pk}")
        self.assertNotContains(again, 'value="99"')
        self.assertContains(again, 'value="10"')

    def test_zero_count_reopens_with_the_zero(self):
        resp = self._receive(self.staff, 0, follow=True)
        self.assertContains(resp, 'at least one received quantity')
        self.assertContains(resp, 'value="0"')
        self.assertNotContains(resp, 'value="10"')

    def test_a_successful_receipt_leaves_no_draft_behind(self):
        self._receive(self.manager, 4)
        self.assertNotIn(f'receive_draft_{self.po.pk}', self.client.session)
        resp = self.client.get(f"{reverse('delivery_list')}?receive={self.po.pk}")
        self.assertContains(resp, 'value="6"')

    def test_receive_modal_keeps_counts_across_add_unexpected(self):
        self.client.force_login(self.staff)
        resp = self.client.get(f"{reverse('delivery_list')}?receive={self.po.pk}")
        self.assertContains(resp, f"receive-draft-{self.po.pk}")
