"""Evaluator #4 — fewer clicks between one walk-in customer and the next.

Record Sale lands on the order's detail page. Ringing up the next customer
used to mean going back to the Order Console and pressing New Walk-in Sale
every time; the detail page and its receipt now offer that directly. Cash
tender gets a one-tap "Exact amount".
"""
from django.test import TestCase
from django.urls import reverse

from accounts.models import StaffProfile, User
from branches.models import Branch
from core.module_permissions import DEFAULT_STAFF_MODULES
from inventory.models import Inventory, Product
from sales.models import SalesTransaction


class NextWalkInSaleShortcutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pf-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='PF Branch', location='Baliuag', owner=cls.owner)
        cls.product = Product.objects.create(
            sku_code='PF-1', product_name='Cooking Oil',
            unit_of_measure='Bottle', unit_price=100)
        Inventory.objects.create(
            product=cls.product, branch=cls.branch,
            quantity_on_hand=50, reorder_level=5)
        cls.staff = User.objects.create_user(
            username='pf-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=cls.staff, defaults={'staff_name': 'Pia', 'branch': cls.branch})
        cls.staff.allowed_modules = list(DEFAULT_STAFF_MODULES)
        cls.staff.save(update_fields=['allowed_modules'])

    def _ring_cash_sale(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse('transaction_add'), {
            'mode_of_payment': 'Cash', 'amount_tendered': '100',
            'product_id': [str(self.product.pk)], 'quantity': ['1'],
        })
        txn = SalesTransaction.objects.get()
        self.assertRedirects(resp, reverse('transaction_detail', args=[txn.pk]),
                             fetch_redirect_response=False)
        return txn

    def test_detail_after_record_sale_offers_the_next_sale(self):
        txn = self._ring_cash_sale()
        resp = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertContains(resp, 'New Walk-in Sale')
        self.assertContains(resp, f'href="{reverse("transaction_add")}"')

    def test_receipt_offers_the_next_sale(self):
        txn = self._ring_cash_sale()
        resp = self.client.get(reverse('transaction_receipt', args=[txn.pk]))
        self.assertContains(resp, f'href="{reverse("transaction_add")}"')

    def test_not_offered_inside_the_order_console_overlay(self):
        txn = self._ring_cash_sale()
        resp = self.client.get(reverse('transaction_detail', args=[txn.pk]), {'modal': '1'})
        self.assertNotContains(resp, f'href="{reverse("transaction_add")}"')

    def test_not_offered_on_online_orders(self):
        txn = SalesTransaction.objects.create(
            branch=self.branch, total_amount=100,
            transaction_type=SalesTransaction.TransactionType.ONLINE)
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('transaction_detail', args=[txn.pk]))
        self.assertNotContains(resp, f'href="{reverse("transaction_add")}"')

    def test_pos_form_offers_exact_amount_tender(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('transaction_add'))
        self.assertContains(resp, 'id="pos-exact-btn"')
