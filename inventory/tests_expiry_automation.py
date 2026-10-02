from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Notification, User
from branches.models import Branch
from inventory.models import (
    Batch, Inventory, Product, StockMovement, Warehouse, WarehouseStock,
)
from inventory.serializers import StockMovementSerializer
from inventory.services import ExpiryProcessingError, expire_due_batches


class ExpiryAutomationTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.branch = Branch.objects.create(
            branch_name='Expiry Test Branch', location='Baliuag',
        )
        self.warehouse = Warehouse.objects.get(branch=self.branch)
        self.product = Product.objects.create(
            sku_code='SKU-EXP-AUTO',
            product_name='Expiry Test Product',
            unit_price=Decimal('25.00'),
        )

    def make_batch(self, *, location, quantity, expiry_date, lot=''):
        return Batch.objects.create(
            product=self.product,
            branch=self.branch,
            location=location,
            quantity=quantity,
            received_date=self.today - timedelta(days=10),
            expiry_date=expiry_date,
            external_lot_code=lot,
            notes='Original receiving record',
        )

    def test_expiry_date_is_immediately_non_sellable(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=15,
        )
        due = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
        )
        fresh = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=10,
            expiry_date=self.today + timedelta(days=1),
        )

        inventory = Inventory.objects.sellable().get(
            product=self.product, branch=self.branch,
        )

        self.assertEqual(inventory.available_qty, 10)
        self.assertEqual(due.expiry_status, Batch.ExpiryStatus.EXPIRED)
        self.assertEqual(fresh.expiry_status, Batch.ExpiryStatus.EXPIRING_SOON)

    def test_fefo_never_consumes_an_expired_lot(self):
        expired = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
        )
        fresh = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=10,
            expiry_date=self.today + timedelta(days=2),
        )

        with transaction.atomic():
            consumed = Batch.consume_fefo(
                self.product, self.branch, Batch.Location.FLOOR, 3,
            )

        expired.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual([(batch.pk, qty) for batch, qty in consumed], [(fresh.pk, 3)])
        self.assertEqual(expired.quantity, 5)
        self.assertEqual(fresh.quantity, 7)

    def test_sweep_removes_floor_and_warehouse_lots_and_audits_each_batch(self):
        inventory = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=15,
        )
        warehouse_stock = WarehouseStock.objects.create(
            warehouse=self.warehouse, product=self.product, quantity_on_hand=7,
        )
        floor_due = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
            lot='LOT-FLOOR-1',
        )
        fresh = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=10,
            expiry_date=self.today + timedelta(days=30),
        )
        warehouse_due = self.make_batch(
            location=Batch.Location.WAREHOUSE,
            quantity=7,
            expiry_date=self.today - timedelta(days=1),
            lot='LOT-WH-1',
        )

        result = expire_due_batches(as_of_date=self.today)

        self.assertEqual(result.batch_count, 2)
        self.assertEqual(result.unit_count, 12)
        self.assertEqual(result.floor_unit_count, 5)
        self.assertEqual(result.warehouse_unit_count, 7)
        inventory.refresh_from_db()
        warehouse_stock.refresh_from_db()
        floor_due.refresh_from_db()
        warehouse_due.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(inventory.quantity_on_hand, 10)
        self.assertEqual(warehouse_stock.quantity_on_hand, 0)
        self.assertEqual(floor_due.quantity, 0)
        self.assertEqual(warehouse_due.quantity, 0)
        self.assertEqual(fresh.quantity, 10)
        self.assertEqual(floor_due.notes, 'Original receiving record')
        self.assertEqual(floor_due.external_lot_code, 'LOT-FLOOR-1')

        movements = StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
        ).order_by('batch_id')
        self.assertEqual(movements.count(), 2)
        floor_movement = movements.get(batch=floor_due)
        self.assertEqual(floor_movement.reference_id, floor_due.pk)
        self.assertEqual(floor_movement.quantity_change, -5)
        self.assertEqual(floor_movement.quantity_before, 15)
        self.assertEqual(floor_movement.quantity_after, 10)
        self.assertIsNone(floor_movement.created_by)
        self.assertIn('LOT-FLOOR-1', floor_movement.notes)
        audit_data = StockMovementSerializer(floor_movement).data
        self.assertEqual(audit_data['batchId'], floor_due.pk)
        self.assertEqual(audit_data['batchNumber'], floor_due.batch_number)
        self.assertEqual(audit_data['externalLotCode'], 'LOT-FLOOR-1')
        self.assertEqual(audit_data['batchExpiryDate'], self.today.isoformat())

        second = expire_due_batches(as_of_date=self.today)
        self.assertEqual(second.batch_count, 0)
        self.assertEqual(StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
        ).count(), 2)

    def test_mismatch_aborts_without_partial_disposal(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=5,
        )
        first = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=2,
            expiry_date=self.today,
        )
        second = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=5,
            expiry_date=self.today,
        )

        with self.assertRaises(ExpiryProcessingError):
            expire_due_batches(as_of_date=self.today)

        first.refresh_from_db()
        second.refresh_from_db()
        inventory = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inventory.quantity_on_hand, 5)
        self.assertEqual(first.quantity, 2)
        self.assertEqual(second.quantity, 5)
        self.assertFalse(StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
        ).exists())

    def test_daily_command_runs_the_expiry_sweep(self):
        inventory = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=4,
        )
        batch = self.make_batch(
            location=Batch.Location.FLOOR,
            quantity=4,
            expiry_date=self.today,
        )
        output = StringIO()

        call_command('dispatch_notifications', stdout=output)

        inventory.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(inventory.quantity_on_hand, 0)
        self.assertEqual(batch.quantity, 0)
        self.assertIn('Wrote off 1 expired batch (4 units).', output.getvalue())


class ExpiredStockHiddenFromManagementScreensTests(TestCase):
    """An expired batch must disappear from Floor Stock and Warehouse Stock
    immediately — not only once the daily sweep has actually zeroed it out.
    Mirrors what Inventory.sellable() already guaranteed for the POS/
    checkout side; this is the same treatment for the internal management
    screens, which used to show the raw, not-yet-swept aggregate on purpose."""

    def setUp(self):
        self.today = timezone.localdate()
        self.owner = User.objects.create_user(
            username='exp-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Expiry Visibility Branch', location='Baliuag',
        )
        self.warehouse = Warehouse.objects.get(branch=self.branch)
        self.product = Product.objects.create(
            sku_code='SKU-EXP-VIS', product_name='Visibility Test Product',
            unit_price=Decimal('30.00'),
        )
        self.client.force_login(self.owner)

    def _batch(self, location, quantity, expiry_date):
        return Batch.objects.create(
            product=self.product, branch=self.branch, location=location,
            quantity=quantity, received_date=self.today - timedelta(days=5),
            expiry_date=expiry_date,
        )

    # -- Floor Stock ------------------------------------------------------------

    def test_an_expired_floor_batch_is_not_listed_as_a_row(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=8,
        )
        self._batch(Batch.Location.FLOOR, 8, self.today)  # expires today

        response = self.client.get(reverse('inventory_list'))

        rows = response.context['rows']
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['batch'])  # falls back to the "No Stock" empty row
        self.assertEqual(rows[0]['status'], 'No Stock')

    def test_a_fresh_batch_still_shows_when_another_lot_has_expired(self):
        Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=18,
        )
        self._batch(Batch.Location.FLOOR, 8, self.today)
        self._batch(Batch.Location.FLOOR, 10, self.today + timedelta(days=5))

        response = self.client.get(reverse('inventory_list'))

        rows = response.context['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['batch'].quantity, 10)
        self.assertEqual(rows[0]['status'], 'In Stock')

    def test_this_never_touches_the_stored_quantity_on_hand(self):
        """Display-only — the aggregate itself is still whatever it was until
        the actual sweep (or a manual write-off) runs; only what this screen
        shows changes."""
        inv = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity_on_hand=8,
        )
        self._batch(Batch.Location.FLOOR, 8, self.today)

        self.client.get(reverse('inventory_list'))

        inv.refresh_from_db()
        self.assertEqual(inv.quantity_on_hand, 8)

    # -- Warehouse Stock ----------------------------------------------------------

    def test_an_expired_warehouse_batch_is_not_listed_as_a_row(self):
        WarehouseStock.objects.create(
            warehouse=self.warehouse, product=self.product, quantity_on_hand=6,
        )
        self._batch(Batch.Location.WAREHOUSE, 6, self.today)

        response = self.client.get(reverse('warehouse_stock_list'))

        rows = response.context['rows']
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['batch'])
        self.assertEqual(rows[0]['status'], 'No Stock')

    def test_the_request_to_floor_modal_excludes_an_expired_only_product(self):
        WarehouseStock.objects.create(
            warehouse=self.warehouse, product=self.product, quantity_on_hand=6,
        )
        self._batch(Batch.Location.WAREHOUSE, 6, self.today)

        response = self.client.get(
            reverse('warehouse_stock_list'),
            {'open_request': '1', 'branch': self.branch.pk},
        )

        self.assertEqual(list(response.context['request_warehouse_stock']), [])


class ManualExpiryWriteOffViewTests(TestCase):
    """Alerts Center's Expired-tab "Write Off" button
    (inventory.views.expiry_write_off) — must work directly on a warehouse
    lot, not only a floor one. An expired warehouse lot has no business
    being transferred onto the floor just to become eligible for write-off:
    that would briefly present it as sellable stock, which it never was."""

    def setUp(self):
        self.today = timezone.localdate()
        self.owner = User.objects.create_user(
            username='mwo-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Manual Write-off Branch', location='Baliuag',
        )
        self.other_branch = Branch.objects.create(
            branch_name='Manual Write-off Other Branch', location='Baliuag',
        )
        self.warehouse = Warehouse.objects.get(branch=self.branch)
        self.product = Product.objects.create(
            sku_code='SKU-MWO', product_name='Manually Written Off',
            unit_price=Decimal('15.00'),
        )
        self.client.force_login(self.owner)

    def _due_warehouse_batch(self, branch=None, quantity=6):
        branch = branch or self.branch
        warehouse = Warehouse.objects.get(branch=branch)
        WarehouseStock.objects.create(
            warehouse=warehouse, product=self.product, quantity_on_hand=quantity,
        )
        return Batch.objects.create(
            product=self.product, branch=branch, location=Batch.Location.WAREHOUSE,
            quantity=quantity, received_date=self.today - timedelta(days=10),
            expiry_date=self.today - timedelta(days=1),
        )

    def test_a_warehouse_lot_can_be_written_off_directly(self):
        batch = self._due_warehouse_batch(quantity=6)

        response = self.client.post(reverse('expiry_write_off', args=[batch.pk]))

        self.assertRedirects(response, reverse('alerts_center'))
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 0)
        stock = WarehouseStock.objects.get(warehouse=self.warehouse, product=self.product)
        self.assertEqual(stock.quantity_on_hand, 0)
        movement = StockMovement.objects.get(
            movement_type=StockMovement.MovementType.EXPIRY_WRITE_OFF,
        )
        self.assertEqual(movement.created_by, self.owner)  # a manual, attributed action

    def test_a_warehouse_lot_in_another_branch_is_refused(self):
        # Owner is branch-unrestricted by design, so exercise the refusal
        # with a branch-pinned Manager instead.
        manager = User.objects.create_user(
            username='mwo-manager', password='pw', role=User.Role.BRANCH_MANAGER,
        )
        manager.manager_profile.branch = self.branch
        manager.manager_profile.save()
        self.client.force_login(manager)
        batch = self._due_warehouse_batch(branch=self.other_branch, quantity=6)

        response = self.client.post(reverse('expiry_write_off', args=[batch.pk]))

        self.assertRedirects(response, reverse('alerts_center'))
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 6)  # untouched

    def test_default_staff_can_write_off_an_expired_lot_in_their_own_branch(self):
        """KATRINA-RBAC-001: the view has always allowed Staff, but a
        default Staff account's module permissions used to block it before
        the view's own role check was ever reached. Staff created here get
        whatever core.module_permissions.ROLE_DEFAULT_ACTIONS currently
        grants — no custom permission is set up in this test."""
        staff = User.objects.create_user(
            username='mwo-staff', password='pw', role=User.Role.STAFF,
        )
        staff.staff_profile.branch = self.branch
        staff.staff_profile.save()
        self.client.force_login(staff)
        batch = self._due_warehouse_batch(quantity=6)

        response = self.client.post(reverse('expiry_write_off', args=[batch.pk]))

        self.assertRedirects(response, reverse('alerts_center'))
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 0)

    def test_default_staff_is_still_refused_at_another_branch(self):
        """Granting the module action does not relax the existing branch
        safeguard in inventory.views.expiry_write_off."""
        staff = User.objects.create_user(
            username='mwo-staff-other', password='pw', role=User.Role.STAFF,
        )
        staff.staff_profile.branch = self.branch
        staff.staff_profile.save()
        self.client.force_login(staff)
        batch = self._due_warehouse_batch(branch=self.other_branch, quantity=6)

        response = self.client.post(reverse('expiry_write_off', args=[batch.pk]))

        self.assertRedirects(response, reverse('alerts_center'))
        batch.refresh_from_db()
        self.assertEqual(batch.quantity, 6)  # untouched

    def test_the_manual_write_off_still_sends_no_notification(self):
        batch = self._due_warehouse_batch(quantity=6)

        self.client.post(reverse('expiry_write_off', args=[batch.pk]))

        # Quantity hitting zero legitimately raises its own out-of-stock
        # notice (WarehouseStock.save()) — only the write-off event itself
        # is what a manual action must stay silent on.
        self.assertFalse(
            Notification.objects.filter(event_type='expiry_write_off').exists()
        )


class ExpiryWriteOffNotificationTests(TestCase):
    """The unattended daily sweep tells someone what it removed; the manual
    per-batch "Write off" action (Alerts Center's Expired tab) does not —
    see expire_due_batches' own comment for why."""

    def setUp(self):
        self.today = timezone.localdate()
        self.owner = User.objects.create_user(
            username='wo-owner', password='pw', role=User.Role.OWNER
        )
        self.branch = Branch.objects.create(
            branch_name='Write-off Branch', location='Baliuag',
        )
        self.other_branch = Branch.objects.create(
            branch_name='Other Branch', location='Baliuag',
        )
        self.warehouse = Warehouse.objects.get(branch=self.branch)
        self.product = Product.objects.create(
            sku_code='SKU-WO', product_name='Written-off Product',
            unit_price=Decimal('20.00'),
        )
        self.other_product = Product.objects.create(
            sku_code='SKU-WO-2', product_name='Also Written Off',
            unit_price=Decimal('10.00'),
        )

    def _floor_due(self, branch, product, quantity):
        Inventory.objects.update_or_create(
            product=product, branch=branch, defaults={'quantity_on_hand': quantity}
        )
        Batch.objects.create(
            product=product, branch=branch, location=Batch.Location.FLOOR,
            quantity=quantity, received_date=self.today - timedelta(days=5),
            expiry_date=self.today,
        )

    def test_the_automatic_sweep_notifies_the_owner(self):
        self._floor_due(self.branch, self.product, 5)

        expire_due_batches(as_of_date=self.today)

        notice = Notification.objects.get(user=self.owner, event_type='expiry_write_off')
        self.assertTrue(notice.requires_action)  # High priority — stays unresolved
        self.assertIn('Written-off Product', notice.message)
        self.assertIn('Write-off Branch', notice.message)
        self.assertIn('5 unit', notice.message)

    def test_the_manual_write_off_sends_no_notification(self):
        self._floor_due(self.branch, self.product, 5)

        expire_due_batches(as_of_date=self.today, actor=self.owner)

        # A batch due today is also "expiring within 30 days" by definition,
        # so its own near-expiry notice legitimately exists — only the
        # write-off event itself must stay silent for a manual actor.
        self.assertFalse(
            Notification.objects.filter(event_type='expiry_write_off').exists()
        )

    def test_several_batches_in_one_branch_become_one_notification_not_several(self):
        self._floor_due(self.branch, self.product, 5)
        self._floor_due(self.branch, self.other_product, 3)

        expire_due_batches(as_of_date=self.today)

        notices = Notification.objects.filter(user=self.owner, event_type='expiry_write_off')
        self.assertEqual(notices.count(), 1)
        self.assertIn('Written-off Product', notices.get().message)
        self.assertIn('Also Written Off', notices.get().message)
        self.assertIn('8 unit', notices.get().message)  # 5 + 3, combined

    def test_two_branches_produce_two_separate_notifications(self):
        self._floor_due(self.branch, self.product, 5)
        self._floor_due(self.other_branch, self.product, 4)

        expire_due_batches(as_of_date=self.today)

        notices = Notification.objects.filter(user=self.owner, event_type='expiry_write_off')
        self.assertEqual(notices.count(), 2)
        branches_mentioned = {'Write-off Branch' in n.message for n in notices} | \
                             {'Other Branch' in n.message for n in notices}
        self.assertIn(True, branches_mentioned)

    def test_floor_and_warehouse_in_the_same_branch_are_two_notifications(self):
        self._floor_due(self.branch, self.product, 5)
        WarehouseStock.objects.create(
            warehouse=self.warehouse, product=self.other_product, quantity_on_hand=3,
        )
        Batch.objects.create(
            product=self.other_product, branch=self.branch, location=Batch.Location.WAREHOUSE,
            quantity=3, received_date=self.today - timedelta(days=5), expiry_date=self.today,
        )

        expire_due_batches(as_of_date=self.today)

        notices = Notification.objects.filter(user=self.owner, event_type='expiry_write_off')
        self.assertEqual(notices.count(), 2)
        self.assertEqual(
            {n.title.split('(')[-1] for n in notices},
            {'Floor Stock)', 'Warehouse Stock)'},
        )

    def test_nothing_due_sends_nothing(self):
        Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.FLOOR,
            quantity=5, received_date=self.today, expiry_date=self.today + timedelta(days=10),
        )

        expire_due_batches(as_of_date=self.today)

        # The batch itself (expiring in 10 days) still raises its own
        # near-expiry notice on creation — only a write-off notice must
        # stay absent when nothing was actually due.
        self.assertFalse(
            Notification.objects.filter(event_type='expiry_write_off').exists()
        )
