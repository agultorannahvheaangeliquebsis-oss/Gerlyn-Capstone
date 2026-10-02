"""P17 — supplier goods returns: request -> supplier acknowledges -> store ships
(stock leaves the warehouse exactly once) -> settled by credit note or a
replacement delivery received into the warehouse."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import BranchManagerProfile, Notification, StaffProfile, User
from branches.models import Branch
from inventory.models import Batch, Product, StockMovement, Warehouse, WarehouseStock
from procurement.models import (
    DeliveryReceipt, Procurement, ProcurementItem, SupplierReturn, SupplierReturnItem,
)
from procurement.services import (
    ReturnError, cancel_return, create_supplier_return, receive_replacement,
    respond_to_return, returnable_lines, settle_with_credit, ship_return,
)

S = SupplierReturn.Status


class ReturnBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='p17-owner', password='pw', role=User.Role.OWNER)
        cls.branch = Branch.objects.create(
            branch_name='P17 Home', location='Baliuag', owner=cls.owner)
        cls.other_branch = Branch.objects.create(
            branch_name='P17 Other', location='Makinabang', owner=cls.owner)
        cls.manager = cls._staffish('p17-mgr', User.Role.BRANCH_MANAGER, cls.branch)
        cls.other_manager = cls._staffish('p17-mgr2', User.Role.BRANCH_MANAGER, cls.other_branch)
        cls.staff = cls._staffish('p17-staff', User.Role.STAFF, cls.branch)
        cls.supplier = User.objects.create_user(
            username='p17-supplier', password='pw', role=User.Role.SUPPLIER)
        cls.other_supplier = User.objects.create_user(
            username='p17-supplier2', password='pw', role=User.Role.SUPPLIER)
        cls.product = Product.objects.create(
            sku_code='P17-1', product_name='P17 Rice', unit_price=100)
        cls.warehouse = Warehouse.objects.get_or_create(
            branch=cls.branch, defaults={'name': 'P17 Warehouse'})[0]

    @classmethod
    def _staffish(cls, username, role, branch):
        user = User.objects.create_user(username=username, password='pw', role=role)
        if role == User.Role.BRANCH_MANAGER:
            BranchManagerProfile.objects.update_or_create(
                user=user, defaults={'branch': branch, 'manager_name': username, 'contact_number': '0'})
        else:
            StaffProfile.objects.update_or_create(
                user=user, defaults={'branch': branch, 'staff_name': username, 'contact_number': '0'})
        return user

    def setUp(self):
        # A received order: 10 units delivered, all still in the warehouse as
        # two lots (the nearer-expiry one is 4 units).
        self.po = Procurement.objects.create(
            branch=self.branch, supplier=self.supplier,
            order_date=dt.date(2026, 1, 1), status=Procurement.Status.RECEIVED,
            total_cost=0)
        self.item = ProcurementItem.objects.create(
            procurement=self.po, product=self.product,
            quantity_ordered=10, quantity_received=10)
        self.stock = WarehouseStock.objects.create(
            product=self.product, warehouse=self.warehouse, quantity_on_hand=10)
        self.lot_soon = Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.WAREHOUSE,
            procurement=self.po, quantity=4, received_date=dt.date(2026, 1, 2),
            expiry_date=dt.date(2026, 3, 1))
        self.lot_late = Batch.objects.create(
            product=self.product, branch=self.branch, location=Batch.Location.WAREHOUSE,
            procurement=self.po, quantity=6, received_date=dt.date(2026, 1, 2),
            expiry_date=dt.date(2027, 1, 1))

    def make_return(self, qty=3, reason='Damaged', notes='', status=None):
        ret = create_supplier_return(
            self.po, self.manager, reason=reason, notes=notes,
            quantities={self.item.pk: qty})
        if status:
            SupplierReturn.objects.filter(pk=ret.pk).update(status=status)
            ret.refresh_from_db()
        return ret

    def shipped(self, qty=3):
        ret = self.make_return(qty)
        respond_to_return(ret, acknowledge=True)
        return ship_return(ret, self.manager)

    def stock_now(self):
        self.stock.refresh_from_db()
        return self.stock.quantity_on_hand


class CreateTests(ReturnBase):
    def test_creates_a_requested_return_with_a_number(self):
        ret = self.make_return(3, notes='crushed')
        self.assertEqual(ret.status, S.REQUESTED)
        self.assertTrue(ret.return_number.startswith('SR-'))
        self.assertEqual(ret.supplier, self.supplier)
        self.assertEqual(ret.branch, self.branch)
        self.assertEqual(ret.requested_by, self.manager)
        self.assertEqual(ret.items.get().quantity_returned, 3)
        self.assertEqual(self.stock_now(), 10)  # nothing moves yet

    def test_only_received_orders_can_be_returned(self):
        for status in (Procurement.Status.DRAFT, Procurement.Status.APPROVED,
                       Procurement.Status.INCOMING, Procurement.Status.CANCELLED):
            Procurement.objects.filter(pk=self.po.pk).update(status=status)
            with self.assertRaises(ReturnError):
                self.make_return()
        Procurement.objects.filter(pk=self.po.pk).update(
            status=Procurement.Status.PARTIALLY_RECEIVED)
        self.assertTrue(self.make_return())

    def test_reason_and_quantities_are_validated(self):
        for kwargs in ({'reason': ''}, {'reason': 'Bad'}, {'reason': 'Other', 'notes': ''}):
            with self.assertRaises(ReturnError):
                create_supplier_return(
                    self.po, self.manager, quantities={self.item.pk: 1},
                    **{'reason': 'Damaged', 'notes': '', **kwargs})
        for quantities in ({}, {self.item.pk: 0}, {self.item.pk: None}):
            with self.assertRaises(ReturnError):
                create_supplier_return(
                    self.po, self.manager, reason='Damaged', notes='', quantities=quantities)
        self.assertFalse(SupplierReturn.objects.exists())

    def test_cannot_return_more_than_was_received(self):
        with self.assertRaises(ReturnError):
            self.make_return(11)

    def test_cannot_return_units_that_left_the_warehouse(self):
        # 8 of the 10 were withdrawn to the floor: only 2 remain returnable.
        Batch.objects.filter(pk=self.lot_soon.pk).update(quantity=0)
        Batch.objects.filter(pk=self.lot_late.pk).update(quantity=2)
        self.assertEqual(returnable_lines(self.po)[0]['max'], 2)
        with self.assertRaises(ReturnError):
            self.make_return(3)
        self.assertTrue(self.make_return(2))

    def test_units_in_an_open_return_cannot_be_claimed_twice(self):
        self.make_return(8)
        self.assertEqual(returnable_lines(self.po)[0]['max'], 2)
        with self.assertRaises(ReturnError):
            self.make_return(3)

    def test_rejected_and_cancelled_returns_free_their_units(self):
        first = self.make_return(8)
        respond_to_return(first, acknowledge=False, note='no')
        self.assertEqual(returnable_lines(self.po)[0]['max'], 10)
        second = self.make_return(8)
        cancel_return(second)
        self.assertEqual(returnable_lines(self.po)[0]['max'], 10)

    def test_a_shipped_return_permanently_uses_up_those_units(self):
        self.shipped(4)
        row = returnable_lines(self.po)[0]
        self.assertEqual((row['claimed'], row['in_warehouse'], row['max']), (4, 6, 6))

    def test_an_unexpected_line_with_nothing_received_is_not_offered(self):
        other = Product.objects.create(sku_code='P17-2', product_name='P17 Extra', unit_price=5)
        ProcurementItem.objects.create(
            procurement=self.po, product=other, quantity_ordered=0, is_unexpected=True)
        self.assertEqual([r['item'].product for r in returnable_lines(self.po)], [self.product])


class WorkflowTests(ReturnBase):
    def test_supplier_can_acknowledge_once(self):
        ret = self.make_return()
        respond_to_return(ret, acknowledge=True)
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.ACKNOWLEDGED)
        self.assertIsNotNone(ret.responded_at)
        with self.assertRaises(ReturnError):
            respond_to_return(ret, acknowledge=False, note='changed my mind')

    def test_rejecting_needs_a_reason_and_records_it(self):
        ret = self.make_return()
        with self.assertRaises(ReturnError):
            respond_to_return(ret, acknowledge=False, note='  ')
        respond_to_return(ret, acknowledge=False, note='Not our fault')
        ret.refresh_from_db()
        self.assertEqual((ret.status, ret.supplier_note), (S.REJECTED, 'Not our fault'))

    def test_shipping_takes_stock_out_of_the_warehouse_exactly_once(self):
        ret = self.make_return(6)
        with self.assertRaises(ReturnError):        # not acknowledged yet
            ship_return(ret, self.manager)
        respond_to_return(ret, acknowledge=True)
        ship_return(ret, self.manager, note='LBC 123')
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.SHIPPED)
        self.assertEqual((ret.shipped_by, ret.shipping_note), (self.manager, 'LBC 123'))
        self.assertEqual(self.stock_now(), 4)
        with self.assertRaises(ReturnError):        # second click does nothing
            ship_return(ret, self.manager)
        self.assertEqual(self.stock_now(), 4)

    def test_units_come_off_this_orders_nearest_expiry_lot_first(self):
        self.shipped(6)
        self.lot_soon.refresh_from_db()
        self.lot_late.refresh_from_db()
        self.assertEqual((self.lot_soon.quantity, self.lot_late.quantity), (0, 4))

    def test_every_batch_touched_gets_an_audited_stock_movement(self):
        ret = self.shipped(6)
        moves = list(StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.SUPPLIER_RETURN).order_by('pk'))
        self.assertEqual([m.quantity_change for m in moves], [-4, -2])
        self.assertEqual([(m.quantity_before, m.quantity_after) for m in moves], [(10, 6), (6, 4)])
        self.assertEqual({m.created_by for m in moves}, {self.manager})
        self.assertEqual({m.reference_id for m in moves}, {ret.pk})
        self.assertEqual({m.warehouse_stock for m in moves}, {self.stock})
        self.assertEqual(moves[0].batch, self.lot_soon)
        self.assertIn(ret.return_number, moves[0].notes)

    def test_expired_lots_can_be_returned(self):
        Batch.objects.filter(pk=self.lot_soon.pk).update(expiry_date=dt.date(2020, 1, 1))
        self.shipped(4)
        self.lot_soon.refresh_from_db()
        self.assertEqual(self.lot_soon.quantity, 0)

    def test_shipping_is_all_or_nothing_when_stock_has_gone(self):
        ret = self.make_return(5)
        respond_to_return(ret, acknowledge=True)
        # Someone moved most of it to the floor after the request.
        Batch.objects.filter(pk=self.lot_soon.pk).update(quantity=0)
        Batch.objects.filter(pk=self.lot_late.pk).update(quantity=2)
        WarehouseStock.objects.filter(pk=self.stock.pk).update(quantity_on_hand=2)
        with self.assertRaises(ReturnError):
            ship_return(ret, self.manager)
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.ACKNOWLEDGED)
        self.assertEqual(self.stock_now(), 2)
        self.assertFalse(StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.SUPPLIER_RETURN).exists())

    def test_cancel_is_allowed_before_shipping_only(self):
        for status in (S.REQUESTED, S.ACKNOWLEDGED, S.REJECTED):
            ret = self.make_return(1, status=status)
            cancel_return(ret)
            ret.refresh_from_db()
            self.assertEqual(ret.status, S.CANCELLED)
        shipped = self.shipped(1)
        with self.assertRaises(ReturnError):
            cancel_return(shipped)

    def test_credit_note_settles_a_shipped_return(self):
        ret = self.shipped(2)
        for ref, amount in (('', '10'), ('CN-1', ''), ('CN-1', '0'), ('CN-1', '-5'), ('CN-1', 'abc')):
            with self.assertRaises(ReturnError):
                settle_with_credit(ret, self.manager, reference=ref, amount=amount)
        settle_with_credit(ret, self.manager, reference='CN-77', amount='150.505')
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.SETTLED)
        self.assertEqual(ret.resolution, 'Credit Note')
        self.assertEqual((ret.credit_reference, ret.credit_amount), ('CN-77', Decimal('150.51')))
        self.assertEqual((ret.settled_by, ret.settled_at is not None), (self.manager, True))

    def test_credit_needs_a_shipped_return(self):
        ret = self.make_return()
        with self.assertRaises(ReturnError):
            settle_with_credit(ret, self.manager, reference='CN', amount='5')

    def test_replacement_receipt_adds_stock_and_settles_when_complete(self):
        ret = self.shipped(5)                     # warehouse now 5
        line = ret.items.get()
        with self.assertRaises(ReturnError):      # more than is owed
            receive_replacement(ret, self.manager, lines={line.pk: (6, None)})
        with self.assertRaises(ReturnError):      # nothing entered
            receive_replacement(ret, self.manager, lines={line.pk: (0, None)})

        receive_replacement(
            ret, self.manager, lines={line.pk: (2, dt.date(2027, 6, 1))}, delivered_by='Ben')
        ret.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((ret.status, line.quantity_replaced), (S.SHIPPED, 2))
        self.assertEqual(self.stock_now(), 7)
        batch = Batch.objects.get(procurement=self.po, quantity=2, expiry_date=dt.date(2027, 6, 1))
        self.assertEqual(batch.location, Batch.Location.WAREHOUSE)
        move = StockMovement.objects.get(batch=batch)
        self.assertEqual(move.movement_type, StockMovement.MovementType.PROCUREMENT_RECEIVED)
        self.assertEqual((move.quantity_change, move.quantity_before, move.quantity_after), (2, 5, 7))
        self.assertEqual(move.created_by, self.manager)

        receive_replacement(ret, self.manager, lines={line.pk: (3, None)})
        ret.refresh_from_db()
        self.assertEqual((ret.status, ret.resolution), (S.SETTLED, 'Replacement'))
        self.assertEqual(self.stock_now(), 10)
        receipts = DeliveryReceipt.objects.filter(supplier_return=ret)
        self.assertEqual(receipts.count(), 2)
        self.assertEqual(receipts.first().received_by, self.manager)
        self.assertEqual(receipts.first().delivered_by, 'Ben')
        with self.assertRaises(ReturnError):      # settled: no more receipts
            receive_replacement(ret, self.manager, lines={line.pk: (1, None)})

    def test_replacement_does_not_touch_the_original_receipt_counts(self):
        ret = self.shipped(2)
        receive_replacement(ret, self.manager, lines={ret.items.get().pk: (2, None)})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_received, 10)


class ViewTests(ReturnBase):
    def post(self, user, name, ret, data=None):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=[ret.pk]), data or {})

    def test_manager_can_raise_a_return_from_the_form(self):
        self.client.force_login(self.manager)
        page = self.client.get(reverse('supplier_return_add', args=[self.po.pk]))
        self.assertContains(page, 'P17 Rice')
        self.assertContains(page, 'up to 10')
        response = self.client.post(reverse('supplier_return_add', args=[self.po.pk]), {
            f'qty_{self.item.pk}': '3', 'reason': 'Damaged', 'notes': 'torn sacks'})
        ret = SupplierReturn.objects.get()
        self.assertRedirects(response, reverse('supplier_return_detail', args=[ret.pk]))
        self.assertTrue(Notification.objects.filter(
            user=self.supplier, event_type='supplier_return_for_supplier').exists())

    def test_form_errors_keep_what_was_typed(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('supplier_return_add', args=[self.po.pk]), {
            f'qty_{self.item.pk}': '99', 'reason': 'Damaged'})
        self.assertContains(response, 'at most 10')
        self.assertContains(response, 'value="99"')
        self.assertFalse(SupplierReturn.objects.exists())

    def test_form_needs_a_received_order_and_the_right_role_and_branch(self):
        self.client.force_login(self.other_manager)
        self.client.post(reverse('supplier_return_add', args=[self.po.pk]), {
            f'qty_{self.item.pk}': '1', 'reason': 'Damaged'})
        self.client.force_login(self.staff)
        self.client.post(reverse('supplier_return_add', args=[self.po.pk]), {
            f'qty_{self.item.pk}': '1', 'reason': 'Damaged'})
        self.client.force_login(self.supplier)
        self.client.post(reverse('supplier_return_add', args=[self.po.pk]), {
            f'qty_{self.item.pk}': '1', 'reason': 'Damaged'})
        self.assertFalse(SupplierReturn.objects.exists())

    def test_supplier_deliveries_lists_returns_and_answers_them(self):
        # Returns live on the Deliveries page (not the Dashboard) since the
        # Supplier Portal split into three pages — see procurement/views.py's
        # supplier_deliveries.
        ret = self.make_return()
        self.client.force_login(self.supplier)
        page = self.client.get(reverse('supplier_deliveries'))
        self.assertContains(page, ret.return_number)
        self.assertContains(page, 'Acknowledge')
        self.client.post(reverse('supplier_return_respond', args=[ret.pk]),
                         {'decision': 'acknowledge'})
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.ACKNOWLEDGED)
        self.assertTrue(Notification.objects.filter(
            user=self.owner, event_type='supplier_return_response').exists())
        self.assertTrue(Notification.objects.filter(
            user=self.manager, event_type='supplier_return_response').exists())

    def test_a_rejection_is_high_priority_for_the_store(self):
        ret = self.make_return()
        self.client.force_login(self.supplier)
        self.client.post(reverse('supplier_return_respond', args=[ret.pk]),
                         {'decision': 'reject', 'note': 'Not ours'})
        note = Notification.objects.get(user=self.owner, event_type='supplier_return_response')
        self.assertEqual(note.priority, 'high')
        self.assertIn('Not ours', note.message)

    def test_another_supplier_cannot_answer_it(self):
        ret = self.make_return()
        self.client.force_login(self.other_supplier)
        response = self.client.post(reverse('supplier_return_respond', args=[ret.pk]),
                                    {'decision': 'acknowledge'})
        self.assertEqual(response.status_code, 404)
        self.client.force_login(self.manager)
        self.client.post(reverse('supplier_return_respond', args=[ret.pk]),
                         {'decision': 'acknowledge'})
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.REQUESTED)

    def test_ship_and_credit_through_the_views(self):
        ret = self.make_return(3)
        self.post(self.supplier, 'supplier_return_respond', ret, {'decision': 'acknowledge'})
        self.post(self.manager, 'supplier_return_ship', ret, {'shipping_note': 'LBC'})
        self.assertEqual(self.stock_now(), 7)
        self.post(self.manager, 'supplier_return_credit', ret,
                  {'credit_reference': 'CN-9', 'credit_amount': '45.00'})
        ret.refresh_from_db()
        self.assertEqual((ret.status, ret.credit_reference), (S.SETTLED, 'CN-9'))
        titles = list(Notification.objects.filter(
            user=self.supplier, event_type='supplier_return_for_supplier'
        ).values_list('title', flat=True))
        self.assertTrue(any('shipped back' in t for t in titles))
        self.assertTrue(any('credit note' in t for t in titles))

    def test_replacement_through_the_view(self):
        ret = self.shipped(2)
        line = ret.items.get()
        self.post(self.manager, 'supplier_return_replacement', ret, {
            f'qty_{line.pk}': '2', f'expiry_{line.pk}': '2027-05-05', 'delivered_by': 'Ben'})
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.SETTLED)
        self.assertEqual(self.stock_now(), 10)

    def test_a_bad_replacement_expiry_changes_nothing(self):
        ret = self.shipped(2)
        line = ret.items.get()
        self.post(self.manager, 'supplier_return_replacement', ret, {
            f'qty_{line.pk}': '2', f'expiry_{line.pk}': 'nope'})
        self.assertEqual(self.stock_now(), 8)

    def test_staff_and_other_branch_managers_cannot_act(self):
        ret = self.make_return(3, status=S.ACKNOWLEDGED)
        for user in (self.staff, self.supplier):
            self.post(user, 'supplier_return_ship', ret)
        self.assertEqual(self.post(self.other_manager, 'supplier_return_ship', ret).status_code, 404)
        self.assertEqual(self.stock_now(), 10)
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.ACKNOWLEDGED)

    def test_cancel_view(self):
        ret = self.make_return()
        self.post(self.manager, 'supplier_return_cancel', ret)
        ret.refresh_from_db()
        self.assertEqual(ret.status, S.CANCELLED)

    def test_get_is_not_allowed_on_actions(self):
        ret = self.make_return()
        self.client.force_login(self.manager)
        for name in ('supplier_return_ship', 'supplier_return_cancel', 'supplier_return_credit',
                     'supplier_return_replacement', 'supplier_return_respond'):
            self.assertEqual(
                self.client.get(reverse(name, args=[ret.pk])).status_code, 405, name)

    def test_list_and_detail_scoping(self):
        ret = self.make_return()
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse('supplier_return_list')), ret.return_number)
        self.assertContains(self.client.get(reverse('supplier_return_detail', args=[ret.pk])),
                            ret.return_number)
        self.client.force_login(self.other_manager)
        self.assertNotContains(self.client.get(reverse('supplier_return_list')), ret.return_number)
        self.assertEqual(
            self.client.get(reverse('supplier_return_detail', args=[ret.pk])).status_code, 404)
        self.client.force_login(self.supplier)  # suppliers use the portal only
        self.assertEqual(self.client.get(reverse('supplier_return_list')).status_code, 302)

    def test_list_filters_by_status_and_shows_counts(self):
        self.make_return(1)
        self.make_return(1, status=S.CANCELLED)
        self.client.force_login(self.owner)
        page = self.client.get(reverse('supplier_return_list') + '?status=Cancelled')
        self.assertEqual(len(page.context['returns']), 1)
        self.assertEqual(page.context['total_count'], 2)

    def test_detail_offers_only_the_actions_that_fit_the_status(self):
        ret = self.make_return()
        self.client.force_login(self.manager)
        url = reverse('supplier_return_detail', args=[ret.pk])
        self.assertContains(self.client.get(url), 'Cancel this return')
        self.assertNotContains(self.client.get(url), 'Ship goods back')
        SupplierReturn.objects.filter(pk=ret.pk).update(status=S.ACKNOWLEDGED)
        self.assertContains(self.client.get(url), 'Ship goods back')
        SupplierReturn.objects.filter(pk=ret.pk).update(status=S.SHIPPED)
        page = self.client.get(url)
        self.assertContains(page, 'Record credit')
        self.assertContains(page, 'Receive replacement goods')
        self.assertNotContains(page, 'Cancel this return')
        # Staff may inspect own-branch evidence, but cannot manage the return.
        self.client.force_login(self.staff)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Record credit')

    def test_po_detail_links_to_returns(self):
        self.client.force_login(self.manager)
        self.assertContains(
            self.client.get(reverse('procurement_detail', args=[self.po.pk])), 'Return goods')
        Procurement.objects.filter(pk=self.po.pk).update(status=Procurement.Status.INCOMING)
        self.assertNotContains(
            self.client.get(reverse('procurement_detail', args=[self.po.pk])), 'Return goods')

    def test_sidebar_has_a_returns_link(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('procurement_list')), 'Supplier Returns')
