"""Wording and read-only cues around receiving and supplier detail.

Three small dead ends the receiving flow left people in: an unexpected line
that said its cost was "₱0.00" when it was really unrecorded, a Manager
staring at pending offers with no visible reason the buttons were missing,
and a received PO whose unexpected lines were indistinguishable from the
ordered ones.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, User
from branches.models import Branch
from inventory.models import Product
from accounts.models import SupplierProfile
from procurement.models import Procurement, ProcurementItem


class UnexpectedLineIsLabelledTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='rsd-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='RSD Branch', location='Baliuag', owner=cls.owner)
        cls.supplier = User.objects.create_user(
            username='rsd-supplier', password='pw', role=User.Role.SUPPLIER)
        cls.product = Product.objects.create(
            sku_code='SKU-RSD-1', product_name='Ordered Item',
            unit_price=Decimal('10.00'))
        cls.surprise = Product.objects.create(
            sku_code='SKU-RSD-2', product_name='Surprise Item',
            unit_price=Decimal('10.00'))
        cls.po = Procurement.objects.create(
            branch=cls.branch, supplier=cls.supplier, owner=cls.owner,
            order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.RECEIVED, total_cost=Decimal('50.00'))
        ProcurementItem.objects.create(
            procurement=cls.po, product=cls.product,
            quantity_ordered=10, quantity_received=10,
            unit_cost_snapshot=Decimal('5.00'))
        ProcurementItem.objects.create(
            procurement=cls.po, product=cls.surprise,
            quantity_ordered=0, quantity_received=2,
            unit_cost_snapshot=Decimal('4.00'), is_unexpected=True)

    def test_detail_marks_only_the_line_that_was_not_ordered(self):
        self.client.force_login(self.owner)
        html = self.client.get(
            reverse('procurement_detail', args=[self.po.pk])
        ).content.decode()
        self.assertEqual(html.count('Not ordered'), 1)
        # It sits on the surprise row, not the ordered one.
        surprise_row = html.split('Surprise Item', 1)[1][:400]
        self.assertIn('Not ordered', surprise_row)

    def test_receive_modal_no_longer_promises_a_zero_cost(self):
        self.client.force_login(self.owner)
        incoming = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier, owner=self.owner,
            order_date=dt.date(2026, 1, 1),
            status=Procurement.Status.INCOMING, total_cost=Decimal('10.00'))
        ProcurementItem.objects.create(
            procurement=incoming, product=self.product,
            quantity_ordered=2, unit_cost_snapshot=Decimal('5.00'))
        html = self.client.get(
            f"{reverse('delivery_list')}?receive={incoming.pk}"
        ).content.decode()
        self.assertIn('left unconfirmed until someone sets it later', html)
        self.assertNotIn('recorded as ₱0.00', html)


class SupplierDetailReadOnlyCueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='sdro-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='SDRO Branch', location='Baliuag', owner=cls.owner)
        cls.manager = User.objects.create_user(
            username='sdro-mgr', password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=cls.manager,
            defaults={'branch': cls.branch, 'manager_name': 'M', 'contact_number': '0'})
        cls.supplier = User.objects.create_user(
            username='sdro-supplier', password='pw', role=User.Role.SUPPLIER)
        SupplierProfile.objects.update_or_create(
            user=cls.supplier, defaults={'supplier_name': 'SDRO Supply'})
        cls.product = Product.objects.create(
            sku_code='SKU-SDRO-1', product_name='Item', unit_price=Decimal('10.00'))

    def _pending_request(self):
        from procurement.models import SupplierProductRequest
        SupplierProductRequest.objects.create(
            supplier=self.supplier, product_name='Proposed Item',
            proposed_cost=Decimal('5.00'),
            status=SupplierProductRequest.Status.PENDING,
        )

    def test_a_manager_is_told_why_there_are_no_buttons(self):
        self._pending_request()
        self.client.force_login(self.manager)
        html = self.client.get(
            reverse('supplier_detail', args=[self.supplier.pk])
        ).content.decode()
        self.assertIn('Awaiting a decision', html)
        self.assertIn('Only the Owner can approve or reject these.', html)

    def test_the_owner_who_can_act_is_not_told_that(self):
        self._pending_request()
        self.client.force_login(self.owner)
        html = self.client.get(
            reverse('supplier_detail', args=[self.supplier.pk])
        ).content.decode()
        self.assertIn('Awaiting a decision', html)
        self.assertNotIn('Only the Owner can approve or reject these.', html)
