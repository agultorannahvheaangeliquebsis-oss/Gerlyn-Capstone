"""Staff and Branch Managers see whether a delivery came with a location.

A Delivery order carries the typed address always, and a pin only when the
customer shared one. Where there is no pin the Order Console says so in plain
words — "Location not shared. Use the typed address" — instead of leaving the
rider to wonder whether a map link went missing.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, StaffProfile, User
from branches.models import Branch
from core.module_permissions import SALES_POS
from inventory.models import Inventory, Product
from sales.models import SalesItem, SalesTransaction

NOT_SHARED = 'Location not shared. Use the typed address'


class DeliveryLocationDisplayTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='dd-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Tarcan', location='Baliuag', owner=self.owner
        )
        self.manager = self.internal('dd-manager', User.Role.BRANCH_MANAGER)
        self.staff = self.internal('dd-staff', User.Role.STAFF)
        self.customer = User.objects.create_user(
            username='dd-customer', password='pw', role=User.Role.CUSTOMER
        )
        self.product = Product.objects.create(
            sku_code='SKU-DD', product_name='Rice (Sack)',
            unit_of_measure='Sack', unit_price=Decimal('100.00'),
        )
        Inventory.objects.create(
            product=self.product, branch=self.branch,
            quantity_on_hand=50, reorder_level=5,
        )

    def internal(self, username, role):
        user = User.objects.create_user(username=username, password='pw', role=role)
        if role == User.Role.BRANCH_MANAGER:
            BranchManagerProfile.objects.update_or_create(
                user=user, defaults={'manager_name': username, 'branch': self.branch}
            )
        else:
            StaffProfile.objects.update_or_create(
                user=user, defaults={'staff_name': username, 'branch': self.branch}
            )
        User.objects.filter(pk=user.pk).update(allowed_modules=[SALES_POS])
        user.refresh_from_db()
        return user

    def order(self, fulfillment='Delivery', **extra):
        txn = SalesTransaction.objects.create(
            branch=self.branch, customer=self.customer,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PENDING,
            mode_of_payment=SalesTransaction.ModeOfPayment.COD,
            fulfillment_type=fulfillment,
            delivery_address='14 Rizal St., Brgy. Poblacion, Baliuag' if fulfillment == 'Delivery' else '',
            total_amount=Decimal('100.00'),
            **extra,
        )
        SalesItem.objects.create(
            transaction=txn, product=self.product, quantity_sold=1,
            unit_price=Decimal('100.00'),
        )
        return txn

    def detail(self, txn, user):
        self.client.force_login(user)
        return self.client.get(reverse('transaction_detail', args=[txn.pk]))

    def test_a_delivery_without_a_pin_says_the_location_was_not_shared(self):
        txn = self.order()

        for user in (self.staff, self.manager):
            page = self.detail(txn, user)
            self.assertContains(page, NOT_SHARED)
            self.assertContains(page, '14 Rizal St., Brgy. Poblacion, Baliuag')
            self.assertNotContains(page, 'Open in map')

    def test_a_delivery_with_a_pin_shows_it_and_a_map_link(self):
        txn = self.order(
            delivery_latitude=Decimal('14.951300'), delivery_longitude=Decimal('120.900500'),
        )

        for user in (self.staff, self.manager):
            page = self.detail(txn, user)
            self.assertContains(page, 'Pinned location shared')
            self.assertContains(page, '14.9513, 120.9005')
            self.assertContains(page, 'Open in map')
            self.assertContains(page, 'mlat=14.951300&amp;mlon=120.900500')
            self.assertNotContains(page, NOT_SHARED)

    def test_a_pickup_order_shows_neither_line(self):
        txn = self.order(fulfillment='Pickup')

        page = self.detail(txn, self.staff)

        self.assertNotContains(page, NOT_SHARED)
        self.assertNotContains(page, 'Pinned location shared')

    def test_the_contact_number_is_shown_to_staff(self):
        txn = self.order(delivery_contact_number='0917 123 4567')

        page = self.detail(txn, self.staff)

        self.assertContains(page, 'Contact: 0917 123 4567')

    def test_the_map_link_opens_safely(self):
        txn = self.order(
            delivery_latitude=Decimal('14.951300'), delivery_longitude=Decimal('120.900500'),
        )

        page = self.detail(txn, self.staff)

        self.assertContains(page, 'rel="noopener noreferrer"')
