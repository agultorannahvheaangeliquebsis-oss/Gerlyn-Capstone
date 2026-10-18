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

from accounts.models import BranchManagerProfile, StaffProfile, User
from branches.models import Branch
from core.module_permissions import PROCUREMENT
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

    def test_staff_can_open_the_page_the_supplier_list_links_them_to(self):
        # supplier_list links every row's name/product-count/pending-count
        # straight into supplier_detail for Staff too (both views share the
        # same PROCUREMENT view grant). Staff used to land on "Access
        # denied" the moment they followed one of those links.
        staff = User.objects.create_user(
            username='sdro-staff', password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=staff,
            defaults={'branch': self.branch, 'staff_name': 'S', 'contact_number': '0'})
        # Default Staff modules don't include Procurement — grant it the same
        # way an Owner would from User Management, same as other tests that
        # exercise a Staff account against this module.
        staff.refresh_from_db()
        staff.allowed_modules = [*staff.allowed_modules, PROCUREMENT]
        staff.save(update_fields=['allowed_modules'])
        self.client.force_login(staff)
        response = self.client.get(
            reverse('supplier_detail', args=[self.supplier.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn('Access denied', html)
        # Read-only, same as a Branch Manager — no Owner-only controls leak in.
        self.assertNotIn('Edit Supplier', html)
        self.assertNotIn('id="open-add-product-modal"', html)


class SupplierAccountStatusPillTests(TestCase):
    """The Owner can deactivate a supplier from User Management without
    touching anything here — this page should still say so, the same way
    accounts/user_list.html already does for every other managed account."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='sasp-owner', password='pw', role=User.Role.OWNER)

    def _supplier(self, **kwargs):
        supplier = User.objects.create_user(
            username=kwargs.pop('username'), role=User.Role.SUPPLIER, **kwargs)
        SupplierProfile.objects.update_or_create(
            user=supplier, defaults={'supplier_name': 'Pill Supply'})
        return supplier

    def test_active_supplier_shows_the_active_pill(self):
        supplier = self._supplier(username='sasp-active', password='pw')
        self.client.force_login(self.owner)
        html = self.client.get(
            reverse('supplier_detail', args=[supplier.pk])).content.decode()
        self.assertIn('pill-ok">Active', html)

    def test_deactivated_supplier_shows_the_inactive_pill(self):
        supplier = self._supplier(username='sasp-inactive', password='pw')
        supplier.is_active = False
        supplier.save(update_fields=['is_active'])
        self.client.force_login(self.owner)
        html = self.client.get(
            reverse('supplier_detail', args=[supplier.pk])).content.decode()
        self.assertIn('pill-neutral">Inactive', html)

    def test_never_logged_in_supplier_shows_the_pending_invite_pill(self):
        supplier = self._supplier(username='sasp-pending')
        supplier.set_unusable_password()
        supplier.save()
        self.client.force_login(self.owner)
        html = self.client.get(
            reverse('supplier_detail', args=[supplier.pk])).content.decode()
        self.assertIn('pill-warn">Pending Invite', html)


class SupplierDetailPaginationTests(TestCase):
    """Products and Recent Orders each carry their own "Showing x–y of n" line
    and their own page parameter, so paging one leaves the other alone."""

    @classmethod
    def setUpTestData(cls):
        from procurement.models import SupplierProduct
        cls.owner = User.objects.create_user(
            username='sdpg-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='SDPG Branch', location='Baliuag', owner=cls.owner)
        cls.supplier = User.objects.create_user(
            username='sdpg-supplier', password='pw', role=User.Role.SUPPLIER)
        SupplierProfile.objects.update_or_create(
            user=cls.supplier, defaults={'supplier_name': 'SDPG Supply'})
        for i in range(17):
            product = Product.objects.create(
                sku_code=f'SKU-SDPG-{i:02d}', product_name=f'Pager Item {i:02d}',
                unit_price=Decimal('10.00'))
            SupplierProduct.objects.create(
                supplier=cls.supplier, product=product,
                current_cost_price=Decimal('5.00'),
                status=SupplierProduct.Status.APPROVED)
        for i in range(16):
            Procurement.objects.create(
                branch=cls.branch, supplier=cls.supplier, owner=cls.owner,
                order_date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
                status=Procurement.Status.RECEIVED, total_cost=Decimal('10.00'))

    def _get(self, query=''):
        self.client.force_login(self.owner)
        return self.client.get(
            reverse('supplier_detail', args=[self.supplier.pk]) + query)

    def test_both_lists_show_their_own_range_and_total(self):
        response = self._get()
        self.assertContains(response, 'Showing 1–15 of 17 products')
        self.assertContains(response, 'Showing 1–15 of 16 orders')
        self.assertEqual(len(response.context['links']), 15)
        self.assertEqual(len(response.context['recent_orders']), 15)

    def test_paging_one_list_leaves_the_other_on_its_page(self):
        response = self._get('?products_page=2')
        self.assertContains(response, 'Showing 16–17 of 17 products')
        self.assertContains(response, 'Showing 1–15 of 16 orders')
        # The orders pager's links carry the products page along.
        self.assertContains(response, '?products_page=2&amp;orders_page=2')
