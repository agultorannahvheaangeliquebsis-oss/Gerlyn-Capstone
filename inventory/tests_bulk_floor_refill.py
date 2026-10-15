"""Bulk Request Floor Refill (Alerts Center multi-select).

The bulk path must be the single request's path, many times over: same
validations, no stock moved until release, per-item reasons for what could
not be requested, and the rest created in one transaction.

The Owner submit is a TransactionTestCase on purpose, for the same reason as
tests_floor_refill_validation.OwnerSubmitInAutocommitTests: a plain TestCase
hides select_for_update() outside a transaction, which is what an Owner's
auto-approval does.
"""
from datetime import date

from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import User
from audit.context import set_action_label
from audit.models import AuditLog
from branches.models import Branch
from core.module_permissions import ADD, WAREHOUSE_STOCK
from inventory.models import (
    Batch, Inventory, Product, StockMovement, StockTransfer, StockTransferItem,
    WarehouseStock,
)
from inventory.services import (
    StockTransferRequestError, create_bulk_floor_refills,
)


class _BulkFixture:
    """Branch A (the alerts) and Branch B (a second warehouse).

    A floor: P1 4/20 (suggested 36), P2 10/30 (suggested 50), P3 0/10.
    A warehouse: P1 40, P2 60, P3 none.   B warehouse: P1 15, P3 10.
    """

    def build(self):
        # Called outside a request, the service sets the audit action label on
        # a module-level ContextVar that nothing resets. Left alone it would
        # relabel every audit row written by whichever test runs next.
        self.addCleanup(set_action_label, '')
        self.owner = User.objects.create_user(
            username='bk-owner', password='pw', role=User.Role.OWNER,
        )
        self.a = Branch.objects.create(owner=self.owner, branch_name='Branch A', location='A')
        self.b = Branch.objects.create(owner=self.owner, branch_name='Branch B', location='B')
        self.p1 = Product.objects.create(sku_code='P1', product_name='Scotch Brite Foam', unit_price='10.00')
        self.p2 = Product.objects.create(sku_code='P2', product_name='Dishwashing Liquid', unit_price='10.00')
        self.p3 = Product.objects.create(sku_code='P3', product_name='Cooking Oil 1L', unit_price='10.00')

        self._floor(self.a, self.p1, 4, 20)
        self._floor(self.a, self.p2, 10, 30)
        self._floor(self.a, self.p3, 0, 10)
        self._floor(self.b, self.p1, 2, 10)

        self._warehouse(self.a, self.p1, 40)
        self._warehouse(self.a, self.p2, 60)
        self._warehouse(self.b, self.p1, 15)
        self._warehouse(self.b, self.p3, 10)

        self.manager = self._internal('bk-mgr', User.Role.BRANCH_MANAGER, self.a, grant=True)
        self.staff_without = self._internal('bk-staff', User.Role.STAFF, self.a, grant=False)

    def _internal(self, username, role, branch, *, grant):
        user = User.objects.create_user(username=username, password='pw', role=role)
        profile = (
            user.manager_profile if role == User.Role.BRANCH_MANAGER else user.staff_profile
        )
        profile.branch = branch
        profile.save(update_fields=['branch'])
        actions = dict(user.module_actions)
        actions[WAREHOUSE_STOCK] = [ADD] if grant else []
        user.module_actions = actions
        user.save(update_fields=['module_actions'])
        return user

    def _floor(self, branch, product, qty, reorder):
        return Inventory.objects.create(
            branch=branch, product=product, quantity_on_hand=qty, reorder_level=reorder,
        )

    def _warehouse(self, branch, product, qty):
        WarehouseStock.objects.create(
            warehouse=branch.warehouse, product=product, quantity_on_hand=qty,
        )
        Batch.objects.create(
            product=product, branch=branch, location=Batch.Location.WAREHOUSE,
            quantity=qty, received_date=date.today(), expiry_date=date(2030, 1, 1),
        )

    def warehouse_qty(self, branch, product):
        return WarehouseStock.objects.get(
            warehouse=branch.warehouse, product=product,
        ).quantity_on_hand

    def floor_qty(self, branch, product):
        return Inventory.objects.get(branch=branch, product=product).quantity_on_hand

    def post_bulk(self, user, rows, *, sources=None, **extra):
        """rows: [(dest_branch, product, qty_text)]."""
        self.client.force_login(user)
        data = {
            'row': [f'{d.pk}:{p.pk}' for d, p, _q in rows],
            'row_qty': [q for _d, _p, q in rows],
        }
        sources = sources or {}
        dests = list(dict.fromkeys(d.pk for d, _p, _q in rows))
        data['group_dest'] = dests
        data['group_source'] = [sources.get(d, d) for d in dests]
        data.update(extra)
        return self.client.post(reverse('warehouse_stock_bulk_request'), data)

    @staticmethod
    def flashed(response):
        return ' '.join(str(m) for m in response.context['messages'])


class BulkServiceTests(_BulkFixture, TestCase):
    def setUp(self):
        self.build()

    def group(self, dest, items, source=None):
        return {
            'destination_branch_id': dest.pk,
            'source_branch_id': (source or dest).pk,
            'items': [{'product_id': p.pk, 'quantity': q} for p, q in items],
        }

    def test_one_request_per_destination_with_the_edited_quantities(self):
        result = create_bulk_floor_refills(
            actor=self.owner,
            groups=[
                self.group(self.a, [(self.p1, 22), (self.p2, 50)]),
                self.group(self.b, [(self.p1, 5)]),
            ],
        )
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.created), 2)
        by_dest = {t.destination_branch_id: t for t, _ in result.created}
        qty = dict(
            by_dest[self.a.pk].items.values_list('product_id', 'quantity_requested')
        )
        self.assertEqual(qty, {self.p1.pk: 22, self.p2.pk: 50})
        self.assertEqual(
            by_dest[self.b.pk].items.get().quantity_requested, 5,
        )
        for transfer, _lines in result.created:
            self.assertEqual(transfer.status, StockTransfer.Status.PENDING)
            self.assertEqual(
                transfer.purpose, StockTransfer.Purpose.BRANCH_RESTOCK,
            )

    def test_requesting_moves_no_stock(self):
        create_bulk_floor_refills(
            actor=self.owner,
            groups=[self.group(self.a, [(self.p1, 22), (self.p2, 50)])],
        )
        self.assertEqual(self.warehouse_qty(self.a, self.p1), 40)
        self.assertEqual(self.warehouse_qty(self.a, self.p2), 60)
        self.assertEqual(self.floor_qty(self.a, self.p1), 4)
        self.assertFalse(StockMovement.objects.exists())

    def test_failed_items_are_reported_individually_and_the_rest_created(self):
        result = create_bulk_floor_refills(
            actor=self.owner,
            groups=[self.group(self.a, [
                (self.p1, 10),   # fine
                (self.p3, 5),    # nothing in A's warehouse
                (self.p2, 61),   # one more than A's 60
            ])],
        )
        self.assertEqual(len(result.created), 1)
        transfer, lines = result.created[0]
        self.assertEqual([(p.pk, q) for p, q in lines], [(self.p1.pk, 10)])
        self.assertEqual(transfer.items.count(), 1)

        reasons = {f.product_id: f.reason for f in result.failures}
        self.assertIn('No available warehouse stock', reasons[self.p3.pk])
        self.assertIn('Only 60 units are available', reasons[self.p2.pk])

    def test_a_pending_request_for_the_same_product_blocks_a_second(self):
        first = create_bulk_floor_refills(
            actor=self.owner, groups=[self.group(self.a, [(self.p1, 10)])],
        )
        slip = first.created[0][0].slip_number

        second = create_bulk_floor_refills(
            actor=self.owner,
            groups=[self.group(self.a, [(self.p1, 5), (self.p2, 5)])],
        )
        self.assertEqual(len(second.created), 1)
        self.assertEqual(
            [p.pk for p, _q in second.created[0][1]], [self.p2.pk],
        )
        self.assertEqual(len(second.failures), 1)
        self.assertEqual(second.failures[0].product_id, self.p1.pk)
        self.assertIn(slip, second.failures[0].reason)
        self.assertIn('already pending', second.failures[0].reason)

    def test_local_warehouse_must_be_used_first(self):
        # Branch A has P1 locally, so asking Branch B for it is refused;
        # P3 has nothing at A, so B may supply it.
        result = create_bulk_floor_refills(
            actor=self.owner,
            groups=[self.group(self.a, [(self.p1, 5), (self.p3, 5)], source=self.b)],
        )
        self.assertEqual(len(result.created), 1)
        self.assertEqual(
            [p.pk for p, _q in result.created[0][1]], [self.p3.pk],
        )
        self.assertEqual(result.created[0][0].source_branch, self.b)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].product_id, self.p1.pk)
        self.assertIn('Use the Branch A warehouse first', result.failures[0].reason)

    def test_each_source_warehouse_gets_its_own_request(self):
        result = create_bulk_floor_refills(
            actor=self.owner,
            groups=[
                self.group(self.a, [(self.p1, 5)]),
                self.group(self.a, [(self.p3, 5)], source=self.b),
            ],
        )
        # The second group is the same destination, so duplicate lines are
        # impossible; two groups -> two requests, one per source.
        self.assertEqual(len(result.created), 2)
        self.assertEqual(
            {t.source_branch_id for t, _ in result.created}, {self.a.pk, self.b.pk},
        )

    def test_the_same_product_twice_is_reported_not_double_requested(self):
        result = create_bulk_floor_refills(
            actor=self.owner,
            groups=[self.group(self.a, [(self.p1, 5), (self.p1, 6)])],
        )
        self.assertEqual(
            StockTransferItem.objects.filter(product=self.p1).count(), 1,
        )
        self.assertEqual(len(result.failures), 1)
        self.assertIn('more than once', result.failures[0].reason)

    def test_a_group_that_fails_late_rolls_back_alone(self):
        def refuse(transfer):
            raise StockTransferRequestError('Approval step refused.')

        result = create_bulk_floor_refills(
            actor=self.owner,
            groups=[self.group(self.a, [(self.p1, 5)])],
            after_create=refuse,
        )
        self.assertEqual(result.created, [])
        self.assertEqual(StockTransfer.objects.count(), 0)
        self.assertEqual(StockTransferItem.objects.count(), 0)
        self.assertIn('Approval step refused.', result.failures[0].reason)

    def test_a_manager_cannot_request_for_another_branch(self):
        result = create_bulk_floor_refills(
            actor=self.manager,
            groups=[
                self.group(self.a, [(self.p1, 5)]),
                self.group(self.b, [(self.p1, 5)]),
            ],
        )
        self.assertEqual(len(result.created), 1)
        self.assertEqual(result.created[0][0].destination_branch, self.a)
        self.assertEqual(len(result.failures), 1)
        self.assertIn('assigned branch', result.failures[0].reason)
        self.assertFalse(
            StockTransfer.objects.filter(destination_branch=self.b).exists()
        )


class BulkViewTests(_BulkFixture, TestCase):
    def setUp(self):
        self.build()

    def test_submit_creates_the_requests_and_returns_to_alerts(self):
        response = self.post_bulk(self.manager, [
            (self.a, self.p1, '22'), (self.a, self.p2, '50'),
        ])
        self.assertRedirects(
            response, reverse('alerts_center'), fetch_redirect_response=False,
        )
        transfer = StockTransfer.objects.get()
        self.assertEqual(transfer.requested_by, self.manager)
        self.assertEqual(transfer.status, StockTransfer.Status.PENDING)
        self.assertEqual(
            dict(transfer.items.values_list('product_id', 'quantity_requested')),
            {self.p1.pk: 22, self.p2.pk: 50},
        )
        self.assertEqual(self.warehouse_qty(self.a, self.p1), 40)

    def test_partial_failure_reopens_the_modal_with_only_the_failed_rows(self):
        response = self.post_bulk(self.manager, [
            (self.a, self.p1, '22'),   # fine
            (self.a, self.p2, '61'),   # over the 60 available
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockTransfer.objects.count(), 1)
        self.assertEqual(
            list(StockTransferItem.objects.values_list('product_id', flat=True)),
            [self.p1.pk],
        )
        failures = response.context['bulk_failures']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['product'], self.p2.product_name)
        self.assertIn('Only 60 units are available', failures[0]['reason'])
        rows = [
            r for g in response.context['bulk_groups'] for r in g['rows']
        ]
        self.assertEqual([r['product'] for r in rows], [self.p2])
        # What the user typed comes back untouched.
        self.assertEqual(rows[0]['quantity'], '61')
        self.assertContains(response, 'could not be submitted')

    def test_a_non_numeric_or_zero_quantity_is_refused_per_line(self):
        for bad in ('abc', '0', '-3', '1.5', ''):
            response = self.post_bulk(self.manager, [(self.a, self.p1, bad)])
            self.assertEqual(response.status_code, 200, bad)
        self.assertFalse(StockTransfer.objects.exists())

    def test_an_invalid_date_is_a_message_not_a_500(self):
        response = self.post_bulk(
            self.manager, [(self.a, self.p1, '5')], transfer_date='2026-02-30',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('valid date', self.flashed(response))
        self.assertFalse(StockTransfer.objects.exists())

    def test_staff_without_the_warehouse_add_action_cannot_submit(self):
        response = self.post_bulk(self.staff_without, [(self.a, self.p1, '5')])
        # ModulePermissionMiddleware raises PermissionDenied (the styled 403
        # page) for a missing module action; it used to flash and redirect.
        self.assertEqual(response.status_code, 403)
        self.assertFalse(StockTransfer.objects.exists())

    def test_a_manager_cannot_target_another_branch_through_the_post(self):
        response = self.post_bulk(self.manager, [(self.b, self.p1, '5')])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StockTransfer.objects.exists())

    def test_each_request_is_its_own_audit_entry_listing_its_items(self):
        self.post_bulk(
            self.owner,
            [(self.a, self.p1, '22'), (self.a, self.p2, '50'), (self.b, self.p1, '5')],
        )
        self.assertEqual(StockTransfer.objects.count(), 2)

        details = AuditLog.objects.filter(
            summary__startswith='Bulk floor refill',
        )
        self.assertEqual(details.count(), 2)
        # Distinct request ids -> two separate entries in the grouped list.
        self.assertEqual(len({d.request_id for d in details}), 2)

        for_a = next(d for d in details if 'Branch A to Branch A' in d.summary)
        self.assertIn('Scotch Brite Foam x22', for_a.summary)
        self.assertIn('Dishwashing Liquid x50', for_a.summary)
        self.assertEqual(
            sorted(
                (i['product'], i['quantity_requested'])
                for i in for_a.after['items']
            ),
            [('Dishwashing Liquid', 50), ('Scotch Brite Foam', 22)],
        )
        self.assertEqual(for_a.action_label, 'Bulk floor refill request: 2 items, 72 units')
        # The item rows of that request carry the same group + label.
        same_group = AuditLog.objects.filter(request_id=for_a.request_id)
        self.assertTrue(same_group.filter(entity='inventory.StockTransferItem').exists())

    def test_modal_opens_in_bulk_mode_with_live_figures_per_branch(self):
        self.client.force_login(self.owner)
        sel = f'{self.a.pk}:{self.p1.pk},{self.a.pk}:{self.p2.pk},{self.b.pk}:{self.p1.pk}'
        response = self.client.get(
            reverse('warehouse_stock_list'),
            {'open_request': 1, 'bulk': 1, 'sel': sel},
        )
        self.assertEqual(response.status_code, 200)
        groups = response.context['bulk_groups']
        self.assertEqual([g['destination'] for g in groups], [self.a, self.b])
        self.assertEqual(
            [g['source_id'] for g in groups], [self.a.pk, self.b.pk],
        )  # local warehouse first, per group

        row = groups[0]['rows'][0]
        self.assertEqual(row['product'], self.p1)
        self.assertEqual((row['current'], row['reorder'], row['suggested']), (4, 20, 36))
        self.assertEqual(row['quantity'], '36')   # defaults to Suggested
        self.assertEqual(
            row['available_json'],
            f'{{"{self.a.pk}": 40, "{self.b.pk}": 15}}',
        )
        self.assertTrue(response.context['show_request_modal'])
        self.assertContains(response, 'From alert')
        self.assertContains(response, 'name="row_qty"')
        self.assertContains(response, reverse('warehouse_stock_bulk_request'))
        # Opening it requests and moves nothing.
        self.assertFalse(StockTransfer.objects.exists())

    def test_a_pending_refill_is_carried_into_the_modal_as_a_blocker(self):
        create_bulk_floor_refills(
            actor=self.owner,
            groups=[{
                'destination_branch_id': self.a.pk,
                'source_branch_id': self.a.pk,
                'items': [{'product_id': self.p1.pk, 'quantity': 5}],
            }],
        )
        slip = StockTransfer.objects.get().slip_number
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('warehouse_stock_list'),
            {'open_request': 1, 'bulk': 1, 'sel': f'{self.a.pk}:{self.p1.pk}'},
        )
        row = response.context['bulk_groups'][0]['rows'][0]
        self.assertEqual(row['pending_slip'], slip)

    def test_a_manager_only_gets_their_own_branch_groups(self):
        self.client.force_login(self.manager)
        sel = f'{self.a.pk}:{self.p1.pk},{self.b.pk}:{self.p1.pk}'
        response = self.client.get(
            reverse('warehouse_stock_list'),
            {'open_request': 1, 'bulk': 1, 'sel': sel},
        )
        groups = response.context['bulk_groups']
        self.assertEqual([g['destination'] for g in groups], [self.a])

    def test_garbage_selection_is_dropped_not_trusted(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('warehouse_stock_list'),
            {'open_request': 1, 'bulk': 1,
             'sel': f"x:y,1:,:2,{self.a.pk}:{self.p1.pk}';DROP,{self.a.pk}:{self.p2.pk}"},
        )
        self.assertEqual(response.status_code, 200)
        rows = [r['product'] for g in response.context['bulk_groups'] for r in g['rows']]
        self.assertEqual(rows, [self.p2])

    def test_checkboxes_follow_the_warehouse_add_permission(self):
        self.client.force_login(self.owner)
        owner_page = self.client.get(reverse('alerts_center'))
        self.assertContains(owner_page, 'id="bulk-select-all"')
        self.assertContains(owner_page, 'id="bulk-bar"')
        self.assertEqual(
            owner_page.content.decode().count('<input type="checkbox" data-bulk-row'), 4,
        )

        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse('alerts_center')), 'id="bulk-select-all"')

        self.client.force_login(self.staff_without)
        page = self.client.get(reverse('alerts_center'))
        self.assertNotContains(page, 'data-bulk-row')
        self.assertNotContains(page, 'bulk-select-all')
        self.assertNotContains(page, 'id="bulk-bar"')

    def test_a_row_with_an_open_refill_is_marked_requested_and_unselectable(self):
        create_bulk_floor_refills(
            actor=self.owner,
            groups=[{
                'destination_branch_id': self.a.pk,
                'source_branch_id': self.a.pk,
                'items': [{'product_id': self.p1.pk, 'quantity': 5}],
            }],
        )
        transfer = StockTransfer.objects.get()
        self.client.force_login(self.owner)
        html = self.client.get(reverse('alerts_center')).content.decode()

        # Exactly one row (Branch A / P1) carries the marker, linked to its slip.
        self.assertEqual(html.count('data-requested'), 1)
        self.assertIn(
            f'<a href="{reverse("transfer_detail", args=[transfer.pk])}">'
            f'{transfer.slip_number}</a>', html,
        )
        self.assertIn('Pending', html[html.index('data-requested'):][:400])
        # Its checkbox is disabled; the other three stay selectable.
        self.assertEqual(html.count(f'disabled title="Refill {transfer.slip_number}'), 1)
        self.assertEqual(
            html.count('<input type="checkbox" data-bulk-row'), 4,
        )

    def test_the_marker_clears_once_the_refill_is_no_longer_open(self):
        create_bulk_floor_refills(
            actor=self.owner,
            groups=[{
                'destination_branch_id': self.a.pk,
                'source_branch_id': self.a.pk,
                'items': [{'product_id': self.p1.pk, 'quantity': 5}],
            }],
        )
        StockTransfer.objects.update(status=StockTransfer.Status.RELEASED)
        self.client.force_login(self.owner)
        html = self.client.get(reverse('alerts_center')).content.decode()
        self.assertEqual(html.count('data-requested'), 0)

    def test_the_row_button_follows_the_warehouse_add_permission(self):
        self.client.force_login(self.staff_without)
        page = self.client.get(reverse('alerts_center'))
        self.assertEqual(page.status_code, 200)
        # The alerts still list; only the refill controls are withheld.
        self.assertContains(page, self.p1.product_name)
        self.assertNotContains(page, 'open_request=1')
        self.assertNotContains(page, 'Request Floor Refill')

        self.client.force_login(self.manager)
        page = self.client.get(reverse('alerts_center'))
        self.assertContains(page, 'open_request=1')
        self.assertContains(page, 'Request Floor Refill')

    def test_pks_over_a_thousand_are_not_localised_in_the_form(self):
        # USE_THOUSAND_SEPARATOR would render pk 1502 as "1,502" in a value.
        branch = Branch.objects.create(
            pk=1501, owner=self.owner, branch_name='Branch Big', location='Z',
        )
        product = Product.objects.create(
            pk=1502, sku_code='BIG', product_name='Big Pk Item', unit_price='10.00',
        )
        self._floor(branch, product, 1, 10)
        self._warehouse(branch, product, 30)
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse('warehouse_stock_list'),
            {'open_request': 1, 'bulk': 1, 'sel': '1501:1502'},
        )
        html = response.content.decode()
        # Only the modal form: the table behind it legitimately shows
        # localised figures, and none of that is posted.
        form = html[html.index('id="bulk-form"'):html.index('id="bulk-summary"')]
        self.assertIn('value="1501:1502"', form)
        self.assertIn('name="group_dest" value="1501"', form)
        self.assertNotIn('1,501', form)
        self.assertNotIn('1,502', form)

        post = self.post_bulk(self.owner, [(branch, product, '12')])
        self.assertEqual(post.status_code, 302)
        self.assertEqual(
            StockTransferItem.objects.get(product=product).quantity_requested, 12,
        )


class OwnerBulkSubmitInAutocommitTests(_BulkFixture, TransactionTestCase):
    def setUp(self):
        self.build()

    def test_owner_bulk_submit_does_not_500_and_auto_approves_without_moving_stock(self):
        response = self.post_bulk(self.owner, [
            (self.a, self.p1, '22'), (self.a, self.p2, '50'), (self.b, self.p1, '5'),
        ])
        self.assertEqual(response.status_code, 302)
        transfers = StockTransfer.objects.all()
        self.assertEqual(transfers.count(), 2)
        for transfer in transfers:
            self.assertEqual(transfer.status, StockTransfer.Status.APPROVED)
            self.assertEqual(transfer.approved_by, self.owner)
            self.assertTrue(
                all(i.quantity_approved == i.quantity_requested
                    for i in transfer.items.all())
            )
        # Approval grants the approval step only; release is what moves stock.
        self.assertEqual(self.warehouse_qty(self.a, self.p1), 40)
        self.assertEqual(self.floor_qty(self.a, self.p1), 4)
        self.assertFalse(StockMovement.objects.exists())
