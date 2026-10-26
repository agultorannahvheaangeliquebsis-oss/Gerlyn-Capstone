"""Shared fixtures for the damage-report tests (not itself a test module).

Two branches, an Owner, a Manager and a Staff member on each, a customer, two
products with floor and warehouse stock backed by real batches, and a
completed, paid online order. Photo uploads are real in-memory images, and the
photo storage is pointed at a throwaway folder so a test run never writes into
the project's media directory.
"""
import io
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from accounts.models import BranchManagerProfile, StaffProfile, User
from billing.models import BillingRecord
from branches.models import Branch
from damage_reports import services
from damage_reports.models import DamageReport, DamageReportPhoto
from inventory.models import Batch, Inventory, Product, Warehouse, WarehouseStock
from procurement.models import Procurement, ProcurementItem
from sales.models import SalesItem, SalesTransaction

# Fixture accounts are signed in with force_login, never a password, so hashing
# nine of them with PBKDF2 on every test is pure cost.
FAST_HASHER = ['django.contrib.auth.hashers.MD5PasswordHasher']

FLOOR = DamageReport.StockLocation.FLOOR.value
WAREHOUSE = DamageReport.StockLocation.WAREHOUSE.value


def image_bytes(fmt='PNG', size=(16, 16), color=(190, 40, 40)):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, fmt)
    return out.getvalue()


def png(name='dent.png'):
    return SimpleUploadedFile(name, image_bytes('PNG'), content_type='image/png')


def jpg(name='dent.jpg'):
    return SimpleUploadedFile(name, image_bytes('JPEG'), content_type='image/jpeg')


def upload(name, data, content_type='image/png'):
    return SimpleUploadedFile(name, data, content_type=content_type)


class DamageFixtures:
    """Mix into a TestCase / TransactionTestCase and call make_world()."""

    def make_world(self):
        media = tempfile.mkdtemp(prefix='gerlyn-damage-photos-')
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        patcher = patch.object(
            DamageReportPhoto._meta.get_field('image'), 'storage',
            FileSystemStorage(location=media, base_url=None),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.media_root = media

        with self.settings(PASSWORD_HASHERS=FAST_HASHER):
            self.owner = User.objects.create_user(
                username='dr-owner', password='pw', role=User.Role.OWNER)
            self.branch = Branch.objects.create(
                branch_name='Tarcan', location='Baliuag', owner=self.owner)
            self.other_branch = Branch.objects.create(
                branch_name='Makinabang', location='Baliuag', owner=self.owner)
            self.manager = self._manager('dr-manager', self.branch)
            self.other_manager = self._manager('dr-manager2', self.other_branch)
            self.staff = self._staff('dr-staff', self.branch)
            self.coworker = self._staff('dr-staff-b', self.branch)
            self.other_staff = self._staff('dr-staff2', self.other_branch)
            self.customer = User.objects.create_user(
                username='dr-customer', password='pw', role=User.Role.CUSTOMER,
                first_name='Juana', last_name='Cruz')
            self.other_customer = User.objects.create_user(
                username='dr-customer2', password='pw', role=User.Role.CUSTOMER)

        self.product = Product.objects.create(
            sku_code='SKU-DR1', product_name='Canned Sardines 155g',
            unit_of_measure='Can', unit_price=Decimal('20.00'))
        self.product2 = Product.objects.create(
            sku_code='SKU-DR2', product_name='Coffee 3in1 (Box)',
            unit_of_measure='Box', unit_price=Decimal('90.00'))

        today = timezone.localdate()
        self.floor_batch = self.add_stock(
            self.branch, self.product, FLOOR, 30, today + timedelta(days=200))
        self.wh_batch = self.add_stock(
            self.branch, self.product, WAREHOUSE, 40, today + timedelta(days=300))

    # -- people -----------------------------------------------------------------
    def _manager(self, username, branch):
        user = User.objects.create_user(
            username=username, password='pw', role=User.Role.BRANCH_MANAGER)
        BranchManagerProfile.objects.update_or_create(
            user=user, defaults={'manager_name': username, 'branch': branch})
        # The profile the signal cached on this instance predates the branch.
        user.refresh_from_db()
        return user

    def _staff(self, username, branch):
        user = User.objects.create_user(
            username=username, password='pw', role=User.Role.STAFF)
        StaffProfile.objects.update_or_create(
            user=user, defaults={'staff_name': username, 'branch': branch})
        user.refresh_from_db()
        return user

    # -- stock ------------------------------------------------------------------
    def add_stock(self, branch, product, location, quantity, expiry=None,
                  procurement=None):
        """A batch AND its aggregate row, kept in step the way every real
        write path keeps them."""
        if location == FLOOR:
            row, _ = Inventory.objects.get_or_create(
                product=product, branch=branch,
                defaults={'quantity_on_hand': 0, 'reorder_level': 0})
            row.quantity_on_hand += quantity
            row.save()
        else:
            warehouse = Warehouse.objects.get(branch=branch)
            row, _ = WarehouseStock.objects.get_or_create(
                product=product, warehouse=warehouse,
                defaults={'quantity_on_hand': 0, 'reorder_level': 0})
            row.quantity_on_hand += quantity
            row.save()
        return Batch.objects.create(
            product=product, branch=branch, location=location, quantity=quantity,
            received_date=timezone.localdate(), expiry_date=expiry,
            procurement=procurement)

    def on_hand(self, location=FLOOR, branch=None, product=None):
        branch = branch or self.branch
        product = product or self.product
        if location == FLOOR:
            return Inventory.objects.get(branch=branch, product=product).quantity_on_hand
        return WarehouseStock.objects.get(
            warehouse__branch=branch, product=product).quantity_on_hand

    def batch_qty(self, batch):
        batch.refresh_from_db()
        return batch.quantity

    # -- reports ----------------------------------------------------------------
    def file_report(self, user=None, **overrides):
        """File a staff report through the service (defaults: 5 units from the
        floor batch, one photo)."""
        args = dict(
            location=FLOOR, product_id=self.product.pk, batch_id=self.floor_batch.pk,
            quantity='5', damage_type='storage', description='Crushed by a pallet.',
            photos=[png()],
        )
        args.update(overrides)
        user = user or self.staff
        if user.role == User.Role.OWNER and 'branch_id' not in args:
            args['branch_id'] = self.branch.pk
        return services.create_staff_report(user, **args)

    def post_data(self, **overrides):
        data = {
            'stock_key': f'{FLOOR}|{self.product.pk}',
            'batch_id': str(self.floor_batch.pk),
            'quantity': '5',
            'damage_type': 'storage',
            'description': 'Crushed by a pallet.',
            'photos': [png()],
        }
        data.update(overrides)
        return data

    def make_po(self, branch=None, product=None, status=Procurement.Status.RECEIVED):
        po = Procurement.objects.create(
            branch=branch or self.branch, status=status, created_by=self.owner,
            order_date=timezone.localdate())
        ProcurementItem.objects.create(
            procurement=po, product=product or self.product, quantity_ordered=10,
            quantity_received=10, unit_cost_snapshot=Decimal('10.00'))
        return po

    # -- orders -----------------------------------------------------------------
    def make_order(self, *, customer=None, quantity=4, unit_price=Decimal('20.00'),
                   completed_days_ago=1, status=SalesTransaction.OrderStatus.COMPLETED,
                   paid=True, product=None):
        product = product or self.product
        txn = SalesTransaction.objects.create(
            branch=self.branch, order_status=status,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            fulfillment_type=SalesTransaction.FulfillmentType.PICKUP,
            total_amount=quantity * unit_price,
            customer=customer if customer is not None else self.customer,
            completed_at=(
                timezone.now() - timedelta(days=completed_days_ago)
                if status == SalesTransaction.OrderStatus.COMPLETED else None),
        )
        item = SalesItem.objects.create(
            transaction=txn, product=product, quantity_sold=quantity,
            unit_price=unit_price)
        if paid:
            BillingRecord.objects.filter(transaction=txn).update(
                status=BillingRecord.PaymentStatus.PAID)
        return txn, item

    def file_customer_report(self, txn, item, quantity='1', **overrides):
        customer = overrides.pop('customer', self.customer)
        args = dict(
            sales_item_id=item.pk, quantity=quantity,
            description='The can was dented and leaking.', photos=[png()])
        args.update(overrides)
        return services.create_customer_report(customer, txn.pk, **args)


def padded_png(total_bytes):
    """A valid PNG padded with trailing bytes to exactly `total_bytes`."""
    data = image_bytes('PNG')
    return data + b'\0' * (total_bytes - len(data))


def blank_file(name='x.png', size=10):
    return ContentFile(b'\0' * size, name=name)
