from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Notification, User
from billing.models import BillingRecord
from billing.notifications import notify_payment_decision
from branches.models import Branch
from core.module_permissions import (
    ALERTS, BILLING, DELIVERIES, FLOOR_STOCK, WAREHOUSE_STOCK,
)
from core.notifications import (
    EVENT_INCOMING_DELIVERY,
    EVENT_LOW_STOCK,
    EVENT_NEAR_EXPIRY,
    EVENT_ORDER_DELIVERED,
    EVENT_PAYMENT_REJECTED,
    EVENT_PAYMENT_VERIFIED,
    EVENT_PO_APPROVED,
)
from inventory.models import Batch, Inventory, Product, ProductCategory, WarehouseStock
from procurement.models import Procurement
from procurement.notifications import notify_incoming_delivery, notify_po_approved
from sales.models import SalesTransaction
from sales.notifications import notify_order_delivered


class NotificationMatrixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='matrix-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Matrix Branch', location='Baliuag', owner=cls.owner
        )
        cls.manager = User.objects.create_user(
            username='matrix-manager', password='pw',
            role=User.Role.BRANCH_MANAGER,
        )
        cls.manager.manager_profile.branch = cls.branch
        cls.manager.manager_profile.save(update_fields=['branch'])
        cls.manager.allowed_modules = [
            FLOOR_STOCK, WAREHOUSE_STOCK, ALERTS, DELIVERIES, BILLING,
        ]
        cls.manager.save(update_fields=['allowed_modules'])

        cls.staff = User.objects.create_user(
            username='matrix-staff', password='pw', role=User.Role.STAFF
        )
        cls.staff.staff_profile.branch = cls.branch
        cls.staff.staff_profile.save(update_fields=['branch'])
        cls.staff.allowed_modules = [
            FLOOR_STOCK, WAREHOUSE_STOCK, ALERTS, DELIVERIES, BILLING,
        ]
        cls.staff.save(update_fields=['allowed_modules'])

        cls.supplier = User.objects.create_user(
            username='matrix-supplier', password='pw', role=User.Role.SUPPLIER
        )
        cls.customer = User.objects.create_user(
            username='matrix-customer', password='pw', role=User.Role.CUSTOMER
        )
        cls.category = ProductCategory.objects.create(category_name='Matrix')
        cls.product = Product.objects.create(
            category=cls.category,
            sku_code='MATRIX-001',
            product_name='Matrix Product',
            unit_price=Decimal('25.00'),
        )

    def setUp(self):
        # User creation itself produces a low-priority staff audit notice.
        Notification.objects.all().delete()

    def recipients_for(self, event_type):
        return set(Notification.objects.filter(
            event_type=event_type
        ).values_list('user__username', flat=True))

    def test_floor_and_warehouse_low_stock_crossings_follow_module_scope(self):
        floor = Inventory.objects.create(
            branch=self.branch, product=self.product,
            quantity_on_hand=11, reorder_level=10,
        )
        floor.quantity_on_hand = 10
        floor.save()

        self.assertEqual(
            self.recipients_for(EVENT_LOW_STOCK),
            {self.owner.username, self.manager.username, self.staff.username},
        )
        floor_notice = Notification.objects.filter(
            event_type=EVENT_LOW_STOCK, user=self.owner
        ).get()
        self.assertEqual(floor_notice.recipient_role, User.Role.OWNER)
        self.assertEqual(floor_notice.reference_type, 'inventory')
        self.assertEqual(floor_notice.reference_id, str(floor.pk))

        Notification.objects.all().delete()
        warehouse_stock = WarehouseStock.objects.create(
            warehouse=self.branch.warehouse, product=self.product,
            quantity_on_hand=11, reorder_level=10,
        )
        warehouse_stock.quantity_on_hand = 0
        warehouse_stock.save()
        self.assertEqual(
            self.recipients_for(EVENT_LOW_STOCK),
            {self.owner.username, self.manager.username, self.staff.username},
        )
        self.assertTrue(Notification.objects.filter(
            event_type=EVENT_LOW_STOCK,
            reference_type='warehouse_stock',
            reference_id=str(warehouse_stock.pk),
            priority=Notification.Priority.CRITICAL,
        ).exists())

    def test_near_expiry_is_immediate_and_daily_dispatch_is_idempotent(self):
        batch = Batch.objects.create(
            product=self.product,
            branch=self.branch,
            location=Batch.Location.WAREHOUSE,
            quantity=5,
            received_date=timezone.localdate(),
            expiry_date=timezone.localdate() + timedelta(days=10),
        )
        self.assertEqual(
            self.recipients_for(EVENT_NEAR_EXPIRY),
            {self.owner.username, self.manager.username, self.staff.username},
        )
        self.assertEqual(
            Notification.objects.filter(
                event_type=EVENT_NEAR_EXPIRY,
                reference_type='batch', reference_id=str(batch.pk),
            ).count(),
            3,
        )

        call_command('dispatch_notifications', verbosity=0)
        call_command('dispatch_notifications', verbosity=0)
        self.assertEqual(
            Notification.objects.filter(
                event_type=EVENT_NEAR_EXPIRY,
                reference_type='batch', reference_id=str(batch.pk),
            ).count(),
            3,
        )

    def test_po_approval_notifies_requester_and_supplier_not_approver(self):
        procurement = Procurement.objects.create(
            branch=self.branch,
            supplier=self.supplier,
            created_by=self.manager,
            order_date=timezone.localdate(),
            status=Procurement.Status.APPROVED,
        )
        notify_po_approved(procurement, self.owner)

        self.assertEqual(
            self.recipients_for(EVENT_PO_APPROVED),
            {self.manager.username, self.supplier.username},
        )
        self.assertEqual(
            Notification.objects.get(
                event_type=EVENT_PO_APPROVED, user=self.supplier
            ).action_url,
            '/procurement/portal/',
        )

    def test_supplier_delivery_date_notifies_branch_delivery_users(self):
        procurement = Procurement.objects.create(
            branch=self.branch,
            supplier=self.supplier,
            created_by=self.manager,
            order_date=timezone.localdate(),
            delivery_date=timezone.localdate() + timedelta(days=3),
            status=Procurement.Status.APPROVED,
        )
        notify_incoming_delivery(procurement, self.supplier)

        self.assertEqual(
            self.recipients_for(EVENT_INCOMING_DELIVERY),
            {self.owner.username, self.manager.username, self.staff.username},
        )

    def make_delivery_order(self):
        return SalesTransaction.objects.create(
            branch=self.branch,
            customer=self.customer,
            total_amount=Decimal('500.00'),
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            fulfillment_type=SalesTransaction.FulfillmentType.DELIVERY,
            delivery_address='1 Matrix Street',
            order_status=SalesTransaction.OrderStatus.COMPLETED,
            mode_of_payment=SalesTransaction.ModeOfPayment.GCASH,
        )

    def test_delivered_order_and_verified_payment_notify_customer(self):
        order = self.make_delivery_order()
        Notification.objects.all().delete()

        notify_order_delivered(order, self.staff)
        bill = order.billing_record
        bill.status = BillingRecord.PaymentStatus.PAID
        bill.save(update_fields=['status'])
        notify_payment_decision(bill, self.manager)

        self.assertEqual(
            self.recipients_for(EVENT_ORDER_DELIVERED), {self.customer.username}
        )
        self.assertEqual(
            self.recipients_for(EVENT_PAYMENT_VERIFIED), {self.customer.username}
        )
        self.assertFalse(Notification.objects.get(
            event_type=EVENT_PAYMENT_VERIFIED
        ).requires_action)

    def test_rejected_payment_reaches_customer_and_other_branch_reviewers(self):
        order = self.make_delivery_order()
        Notification.objects.all().delete()
        bill = order.billing_record
        bill.status = BillingRecord.PaymentStatus.REJECTED
        bill.save(update_fields=['status'])

        notify_payment_decision(bill, self.manager)

        self.assertEqual(
            self.recipients_for(EVENT_PAYMENT_REJECTED),
            {self.owner.username, self.staff.username, self.customer.username},
        )
        self.assertTrue(Notification.objects.get(
            event_type=EVENT_PAYMENT_REJECTED, user=self.owner
        ).requires_action)
        self.assertFalse(Notification.objects.get(
            event_type=EVENT_PAYMENT_REJECTED, user=self.customer
        ).requires_action)
