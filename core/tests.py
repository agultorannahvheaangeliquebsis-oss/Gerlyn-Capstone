"""
Acceptance tests for the five end-to-end scenarios in the build plan.

Run with:
    python manage.py test core

These are the checks the ISO 25010 evaluation leans on for Functional
Suitability, Reliability, and Security — so they must stay green.
"""
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import Address, User
from branches.models import Branch
from core.scoping import scope_queryset
from inventory.models import (
    Inventory, Product, StockMovement, StockTransfer, StockTransferItem,
    Warehouse, WarehouseStock,
)
from procurement.models import Procurement, ProcurementItem
from sales.models import SalesTransaction


class TemplateHygieneTests(TestCase):
    """`{# #}` is single-line only. Spanning one across two lines does not
    make a comment — Django emits the text, and the browser parses any tag
    name inside it as a real element. That has broken the layout three times
    now (Order Management's tabs, the Alerts Center tab row, and the internal
    topbar, where a `<body>` inside the prose opened a second body element).

    It survives every other check: the template compiles, `manage.py check`
    passes, and `get_template()` succeeds. Only rendering shows it, which is
    why it keeps reaching the browser. So it is a test.
    """

    def test_no_multiline_django_comments(self):
        offenders = []
        for root in settings.TEMPLATES[0]['DIRS']:
            for path in Path(root).rglob('*.html'):
                for number, line in enumerate(
                    path.read_text(encoding='utf-8').splitlines(), 1
                ):
                    for match in re.finditer(r'\{#', line):
                        if '#}' not in line[match.end():]:
                            offenders.append(f'{path}:{number}')

        self.assertEqual(
            offenders, [],
            'Unterminated {# on these lines — use {% comment %}...{% endcomment %} '
            'for anything spanning more than one line:\n  ' + '\n  '.join(offenders),
        )


class AcceptanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def login(self, username):
        client = Client()
        client.force_login(User.objects.get(username=username))
        return client

    # ---------------------------------------------------------- scenario 1
    def test_walk_in_pos_deducts_stock_and_records_payment(self):
        ana = User.objects.get(username='ana')
        product = Product.objects.get(sku_code='SKU-005')
        inv = Inventory.objects.get(product=product, branch=ana.staff_profile.branch)
        before = inv.quantity_on_hand

        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash',
            'product_id': [str(product.pk)],
            'quantity': ['4'],
            'amount_tendered': '10000',
        })

        tx = SalesTransaction.objects.order_by('-id').first()
        inv.refresh_from_db()

        self.assertEqual(inv.quantity_on_hand, before - 4)
        self.assertEqual(tx.billing_record.status, 'Paid')
        self.assertEqual(tx.staff.username, 'ana', 'sale must be attributable')

    # ---------------------------------------------------------- scenario 2
    def test_online_order_deducts_stock_only_once_on_completion(self):
        juan = User.objects.get(username='juan')
        branch = Branch.objects.get(branch_name='Tarcan')
        inv = Inventory.objects.filter(branch=branch, quantity_on_hand__gt=20).first()
        # seed_demo populates some already-expired rows for the Alerts
        # Center's Expired tab, and this lookup could land on one of those —
        # an expired row is no longer sellable (Inventory.objects.sellable()).
        # This test is about the deduct-once behaviour, not expiry, so it
        # clears the field explicitly.
        inv.expiry_date = None
        inv.save()
        before = inv.quantity_on_hand

        # checkout() ships a Delivery order to the account's saved default
        # Address now — it no longer reads a typed delivery_address field —
        # so a Delivery checkout needs one on record first.
        Address.objects.create(user=juan, is_default=True, text='Baliuag')

        customer = self.login('juan')
        session = customer.session
        session['branch_id'] = branch.id
        session['cart'] = {str(inv.product_id): 2}
        session['checkout_token'] = 'test-checkout-token'
        session.save()
        customer.post('/shop/checkout/', {
            'mode_of_payment': 'Cash on Delivery',
            'fulfillment_type': 'Delivery',
            'checkout_token': 'test-checkout-token',
        })

        order = SalesTransaction.objects.filter(customer=juan).order_by('-id').first()
        inv.refresh_from_db()

        self.assertEqual(order.order_status, 'Pending')
        self.assertTrue(hasattr(order, 'billing_record'), 'exactly one billing record')
        self.assertEqual(inv.quantity_on_hand, before, 'stock must not move at checkout')

        staff = self.login('ana')
        for status in ['Approved', 'Preparing', 'Completed']:
            if status == 'Completed':
                # An order can only be completed once its payment is Paid.
                order.billing_record.status = 'Paid'
                order.billing_record.save(update_fields=['status'])
            staff.post(f'/sales/{order.pk}/status/{status}/')

        order.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(order.order_status, 'Completed')
        self.assertEqual(inv.quantity_on_hand, before - 2, 'deducts exactly once')

    def test_illegal_status_transition_is_rejected(self):
        order = SalesTransaction.objects.filter(order_status='Completed').first()
        self.login('ana').post(f'/sales/{order.pk}/status/Pending/')
        order.refresh_from_db()
        self.assertEqual(order.order_status, 'Completed')

    # ---------------------------------------------------------- scenario 3
    def test_short_delivery_flags_discrepancy_and_adds_only_delivered(self):
        po = Procurement.objects.filter(status=Procurement.Status.INCOMING).first()
        items = list(po.items.all())
        # A received delivery credits the branch WAREHOUSE, never floor
        # Inventory — floor stock only increases via an approved, released
        # withdrawal slip (see delivery_receive and the Inventory docstring).
        # This test used to assert against floor Inventory and was never
        # updated when the receive handler was corrected.
        warehouse = Warehouse.objects.get(branch=po.branch)
        ws, _ = WarehouseStock.objects.get_or_create(
            product=items[0].product, warehouse=warehouse,
            defaults={'quantity_on_hand': 0, 'reorder_level': 0},
        )
        before = ws.quantity_on_hand

        self.login('gerlyn').post(f'/procurement/deliveries/{po.pk}/receive/', {
            'item_id': [str(i.pk) for i in items],
            'quantity_received': [str(i.quantity_ordered - 5) for i in items],
        })

        po.refresh_from_db()
        ws.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.PARTIALLY_RECEIVED)
        self.assertEqual(po.receipt_flag, Procurement.ReceiptFlag.PARTIAL)
        self.assertEqual(ws.quantity_on_hand, before + items[0].quantity_ordered - 5)
        self.assertTrue(
            StockMovement.objects.filter(
                reference_id=po.pk, movement_type='Procurement Received'
            ).exists(),
            'receipt must be audited',
        )

    def test_double_receive_is_blocked(self):
        po = Procurement.objects.filter(status=Procurement.Status.INCOMING).first()
        items = list(po.items.all())
        owner = self.login('gerlyn')
        owner.post(f'/procurement/deliveries/{po.pk}/receive/', {
            'item_id': [str(i.pk) for i in items],
            'quantity_received': [str(i.quantity_ordered) for i in items],
        })
        inv = Inventory.objects.get(product=items[0].product, branch=po.branch)
        after_first = inv.quantity_on_hand

        owner.post(f'/procurement/{po.pk}/update-status/', {'status': 'Received'})
        inv.refresh_from_db()
        self.assertEqual(inv.quantity_on_hand, after_first, 'must not add stock twice')

    # ---------------------------------------------------------- scenario 4
    def test_withdrawal_discrepancy_corrects_stock_and_audits(self):
        source = Branch.objects.get(branch_name='Tarcan')
        dest = Branch.objects.get(branch_name='Makinabang')
        product = Product.objects.get(sku_code='SKU-004')
        inv = Inventory.objects.get(product=product, branch=dest)
        Inventory.objects.filter(pk=inv.pk).update(quantity_on_hand=100)

        transfer = StockTransfer.objects.create(
            warehouse=Warehouse.objects.get(branch=source),
            source_branch=source, destination_branch=dest,
            purpose='Branch Restock', status='Released',
        )
        StockTransferItem.objects.create(
            transfer=transfer, product=product,
            quantity_requested=40, quantity_approved=40,
        )

        self.login('ben').post(f'/inventory/withdrawals/{transfer.pk}/flag/', {
            'received_qty': 30, 'flag_reason': '10 damaged in transit',
        })

        transfer.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(transfer.status, 'Discrepancy')
        self.assertEqual(transfer.received_qty, 30)
        self.assertEqual(inv.quantity_on_hand, 90, 'corrected by the 10-unit shortfall')
        self.assertTrue(
            StockMovement.objects.filter(
                reference_id=transfer.pk, movement_type='Adjustment'
            ).exists()
        )

    # ---------------------------------------------------------- scenario 5
    def test_manager_is_scoped_to_own_branch(self):
        carlo = User.objects.get(username='carlo')
        branches = set(
            scope_queryset(SalesTransaction.objects.all(), carlo)
            .values_list('branch__branch_name', flat=True)
        )
        self.assertEqual(branches, {'Tarcan'})

    def test_owner_sees_all_branches(self):
        owner = User.objects.get(username='gerlyn')
        branches = set(
            scope_queryset(SalesTransaction.objects.all(), owner)
            .values_list('branch__branch_name', flat=True)
        )
        self.assertGreater(len(branches), 1)

    def test_staff_cannot_reach_user_management(self):
        self.assertEqual(self.login('ana').get('/manage/users/').status_code, 302)

    def test_staff_cannot_add_products(self):
        self.assertEqual(self.login('ana').get('/inventory/products/add/').status_code, 302)

    def test_cross_branch_write_is_blocked(self):
        # ben is Makinabang staff; this order belongs to Tarcan.
        order = SalesTransaction.objects.filter(
            branch__branch_name='Tarcan', order_status='Approved'
        ).first()
        before = order.order_status
        self.login('ben').post(f'/sales/{order.pk}/status/Preparing/')
        order.refresh_from_db()
        self.assertEqual(order.order_status, before, 'cross-branch write must be blocked')

    # --- detail views must scope too, not just lists ----------------------
    # Regression guard: these all leaked via the URL until 2026-07-17.

    def test_supplier_cannot_open_a_rival_suppliers_po(self):
        supplier = User.objects.get(username='monde')
        rival_po = Procurement.objects.exclude(supplier=supplier).first()
        client = Client()
        client.force_login(supplier)
        self.assertEqual(client.get(f'/procurement/{rival_po.pk}/').status_code, 302)

    def test_supplier_can_open_their_own_po(self):
        supplier = User.objects.get(username='monde')
        own_po = Procurement.objects.filter(supplier=supplier).first()
        client = Client()
        client.force_login(supplier)
        self.assertEqual(client.get(f'/procurement/{own_po.pk}/').status_code, 200)

    def test_manager_cannot_open_another_branchs_po(self):
        foreign = Procurement.objects.exclude(branch__branch_name='Tarcan').first()
        self.assertEqual(
            self.login('carlo').get(f'/procurement/{foreign.pk}/').status_code, 302
        )

    def test_staff_cannot_open_another_branchs_billing_record(self):
        from billing.models import BillingRecord
        record = BillingRecord.objects.filter(
            transaction__branch__branch_name='Tarcan'
        ).first()
        # ben is Makinabang staff
        self.assertEqual(self.login('ben').get(f'/billing/{record.pk}/').status_code, 302)

    def test_customer_cannot_open_another_customers_order(self):
        juan = User.objects.get(username='juan')
        someone_elses = SalesTransaction.objects.exclude(
            customer=juan
        ).exclude(customer=None).first()
        client = Client()
        client.force_login(juan)
        self.assertEqual(
            client.get(f'/shop/confirmation/{someone_elses.pk}/').status_code, 404
        )

    # --- supplier portal + lockout -----------------------------------------
    # A supplier is an OUTSIDE party. They see their own orders and nothing else.
    # Regression guard: /inventory/stock/ showed them all 35 rows until 2026-07-17.

    def test_supplier_cannot_see_store_screens(self):
        client = Client()
        client.force_login(User.objects.get(username='century'))
        for url in ['/sales/', '/inventory/stock/', '/inventory/products/',
                    '/manage/users/']:
            self.assertEqual(
                client.get(url).status_code, 302,
                f'a supplier must not reach {url}',
            )
        # /inventory/alerts/ was migrated to @role_required in 11daab1, which
        # raises PermissionDenied (403) instead of the inline-check redirect
        # the other URLs above still use.
        self.assertEqual(
            client.get('/inventory/alerts/').status_code, 403,
            'a supplier must not reach /inventory/alerts/',
        )

    def test_supplier_portal_shows_only_their_own_orders(self):
        supplier = User.objects.get(username='century')
        client = Client()
        client.force_login(supplier)
        html = client.get('/procurement/portal/').content.decode()

        for po in Procurement.objects.filter(supplier=supplier).exclude(status='Draft'):
            self.assertIn(po.po_number, html)
        # A draft order hasn't been placed yet — the supplier must not see it.
        for po in Procurement.objects.filter(supplier=supplier, status='Draft'):
            self.assertNotIn(po.po_number, html)
        # ...nor anyone else's.
        for po in Procurement.objects.exclude(supplier=supplier)[:3]:
            self.assertNotIn(po.po_number, html)

    def test_supplier_can_propose_and_confirm_a_delivery_date_on_own_order(self):
        """Delivery date is agreed through the negotiation flow now — the
        supplier proposes it, the buyer accepts, and only the supplier's
        own final confirmation locks it in (procurement.views' NEGOTIATION
        section)."""
        supplier = User.objects.get(username='delmonte')
        branch = Branch.objects.filter(procurements__supplier=supplier).first()
        product = Product.objects.filter(sku_code='SKU-009').first()
        po = Procurement.objects.create(
            branch=branch, supplier=supplier,
            order_date=timezone.localdate(), status=Procurement.Status.APPROVED,
            total_cost=0,
        )
        item = ProcurementItem.objects.create(
            procurement=po, product=product, quantity_ordered=10,
        )
        proposed_date = timezone.localdate() + timedelta(days=14)

        client = Client()
        client.force_login(supplier)
        client.post(f'/procurement/portal/{po.pk}/respond/', {
            'action': 'propose', 'delivery_date': proposed_date.isoformat(),
            f'unit_cost_{item.pk}': '23.00',
        })
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.PENDING_OWNER_REVIEW)

        client.force_login(User.objects.get(username='gerlyn'))
        client.post(f'/procurement/{po.pk}/respond/', {'action': 'accept'})

        client.force_login(supplier)
        client.post(f'/procurement/portal/{po.pk}/confirm/')

        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.INCOMING)
        self.assertEqual(po.delivery_date, proposed_date)

    def test_supplier_cannot_mark_their_own_order_received(self):
        """Business rule 13: only the store validates a receipt."""
        supplier = User.objects.get(username='delmonte')
        po = Procurement.objects.filter(
            supplier=supplier, status=Procurement.Status.INCOMING
        ).first()
        client = Client()
        client.force_login(supplier)
        client.post(f'/procurement/{po.pk}/update-status/', {'status': 'Received'})
        po.refresh_from_db()
        self.assertEqual(po.status, Procurement.Status.INCOMING)

    # --- customer order history + tracking ---------------------------------
    # The Context Diagram promises the Customer receives order status back.

    def test_customer_sees_only_their_own_orders(self):
        juan = User.objects.get(username='juan')
        client = Client()
        client.force_login(juan)
        html = client.get('/shop/orders/').content.decode()

        for order in SalesTransaction.objects.filter(customer=juan):
            self.assertIn(order.order_number, html)
        for order in SalesTransaction.objects.exclude(customer=juan).exclude(customer=None):
            self.assertNotIn(order.order_number, html)

    def test_customer_can_track_their_own_order(self):
        juan = User.objects.get(username='juan')
        order = SalesTransaction.objects.filter(customer=juan).first()
        client = Client()
        client.force_login(juan)
        response = client.get(f'/shop/orders/{order.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(order.order_number, response.content.decode())

    def test_customer_cannot_track_someone_elses_order(self):
        juan = User.objects.get(username='juan')
        foreign = SalesTransaction.objects.exclude(
            customer=juan
        ).exclude(customer=None).first()
        client = Client()
        client.force_login(juan)
        self.assertEqual(client.get(f'/shop/orders/{foreign.pk}/').status_code, 404)

    # --- bulk tier pricing --------------------------------------------------
    # Chapter 1 describes Gerlyn as a "retail and wholesale business". The
    # wholesale half is priced by quantity: each product carries bulk tiers,
    # and a line is charged the deepest tier its quantity reaches.

    def _tier_sku_005(self):
        """SKU-005 at ₱48 retail with a single break: 10 or more at ₱41.

        Set explicitly rather than read off seed_demo, so these tests pin the
        pricing rule and not whatever ladder the demo data happens to carry.
        """
        product = Product.objects.get(sku_code='SKU-005')
        product.price_tiers.all().delete()
        product.price_tiers.create(min_quantity=10, unit_price=41)
        return product

    def test_a_quantity_below_the_first_tier_pays_retail(self):
        product = self._tier_sku_005()
        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash',
            'product_id': [str(product.pk)], 'quantity': ['9'],
            'amount_tendered': '10000',
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.items.first().unit_price, product.unit_price)

    def test_reaching_a_tier_pays_the_tier_price(self):
        product = self._tier_sku_005()
        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash',
            'product_id': [str(product.pk)], 'quantity': ['10'],
            'amount_tendered': '10000',
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.items.first().unit_price, 41)
        self.assertEqual(tx.total_amount, 410)

    def test_a_product_with_no_tiers_always_pays_retail(self):
        """Never sell at ₱0 just because a product has no bulk price."""
        product = Product.objects.get(sku_code='SKU-005')
        product.price_tiers.all().delete()

        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash',
            'product_id': [str(product.pk)], 'quantity': ['10'],
            'amount_tendered': '10000',
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.total_amount, product.unit_price * 10)

    def test_two_rows_of_one_product_earn_the_same_break_as_one_row(self):
        """5 + 5 on two rows is a 10-unit sale. Pricing rows one at a time
        would charge retail for what a single row of 10 bills at bulk."""
        product = self._tier_sku_005()
        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash',
            'product_id': [str(product.pk), str(product.pk)],
            'quantity': ['5', '5'],
            'amount_tendered': '10000',
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual([item.unit_price for item in tx.items.all()], [41, 41])
        self.assertEqual(tx.total_amount, 410)

    def test_a_posted_order_type_cannot_buy_a_tier_price(self):
        """The Retail/Wholesale toggle is gone. A cached form or a tampered POST
        still sending one must not reach a bulk price below its quantity."""
        product = self._tier_sku_005()
        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash', 'order_type': 'Wholesale',
            'product_id': [str(product.pk)], 'quantity': ['1'],
            'amount_tendered': '10000',
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.items.first().unit_price, product.unit_price)

    def test_price_over_1000_survives_an_edit(self):
        """
        USE_THOUSAND_SEPARATOR localises template output, so a number input
        would render value="1,345.50" — invalid HTML5, silently blanked by the
        browser, price lost on save. The form must unlocalize.
        """
        product = Product.objects.get(sku_code='SKU-010')
        Product.objects.filter(pk=product.pk).update(unit_price='1345.50')
        product.refresh_from_db()

        client = self.login('gerlyn')
        # GET on the edit URL redirects to the Edit Product modal on the
        # product list page — follow it to reach the rendered form.
        html = client.get(f'/inventory/products/edit/{product.pk}/', follow=True).content.decode()
        self.assertIn('value="1345.50"', html, 'number input must not be localised')
        self.assertNotIn('value="1,345.50"', html)

    def test_csv_export_is_branch_scoped(self):
        owner_rows = self.login('gerlyn').get('/reports/export/sales.csv').content.count(b'\n')
        manager_rows = self.login('carlo').get('/reports/export/sales.csv').content.count(b'\n')
        self.assertGreater(owner_rows, manager_rows)

    def test_staff_cannot_export_reports(self):
        response = self.login('ana').get('/reports/export/sales.csv')
        self.assertNotIn('text/csv', response['Content-Type'])


class DetailModalTests(TestCase):
    """The "see" action on every list opens the record over the list.

    What the overlay shows is the detail page's own content block, asked for
    with ?modal=1 (core.context_processors.detail_modal swaps the base
    template). Two things have to keep holding for that to work, and neither
    is visible from the view code alone:

      - the fragment must be a fragment. A detail template that goes back to
        `{% extends "internal_base.html" %}` by name still renders and still
        looks right on its own page, so nothing fails — the overlay just
        quietly fills with a second copy of the whole app shell, sidebar and
        all.
      - ?modal=1 must not become a way around the branch checks. It is a
        rendering flag, nothing else, so a refused record is still refused.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)
        # seed_demo stops short of withdrawal slips, and Stock Withdrawals is
        # the list this started from — so one is made here rather than left
        # as the one module the check does not cover.
        source = Branch.objects.get(branch_name='Tarcan')
        cls.transfer = StockTransfer.objects.create(
            warehouse=Warehouse.objects.get(branch=source),
            source_branch=source,
            destination_branch=Branch.objects.get(branch_name='Makinabang'),
            purpose='Branch Restock', status='Released',
        )
        StockTransferItem.objects.create(
            transfer=cls.transfer,
            product=Product.objects.get(sku_code='SKU-004'),
            quantity_requested=5, quantity_approved=5,
        )

    def login(self, username):
        client = Client()
        client.force_login(User.objects.get(username=username))
        return client

    def detail_urls(self):
        from billing.models import BillingRecord
        return [
            f'/inventory/withdrawals/{self.transfer.pk}/',
            f'/procurement/{Procurement.objects.first().pk}/',
            f'/sales/{SalesTransaction.objects.first().pk}/',
            f'/billing/{BillingRecord.objects.first().pk}/',
            # The printable PO is reached from the detail modal's "Print PO"
            # button, so it has to answer ?modal=1 as a fragment too — it
            # extends base_template rather than internal_base.html by name.
            f'/procurement/{Procurement.objects.first().pk}/print/',
            # The sales receipt is the same story: reached from "Print
            # Receipt" on the order detail, so it also has to be a fragment
            # under ?modal=1.
            f'/sales/{SalesTransaction.objects.first().pk}/receipt/',
        ]

    def test_modal_flag_returns_a_fragment(self):
        owner = self.login('gerlyn')
        for url in self.detail_urls():
            html = owner.get(url, {'modal': '1'}).content.decode()
            self.assertNotIn('<!DOCTYPE', html, f'{url} returned a whole page')
            self.assertNotIn('id="sidebar"', html, f'{url} returned the app shell')
            # detail-modal.js lifts this into the modal's own head; without it
            # the overlay would be titled "Details" and the record would be
            # unnamed on screen.
            self.assertIn('<h1', html, f'{url} has no heading to lift')

    def test_same_url_without_the_flag_is_still_a_page(self):
        owner = self.login('gerlyn')
        for url in self.detail_urls():
            html = owner.get(url).content.decode()
            self.assertIn('<!DOCTYPE', html, f'{url} stopped rendering as a page')
            self.assertIn('id="sidebar"', html, f'{url} lost the app shell')

    def test_modal_flag_does_not_bypass_branch_scoping(self):
        # carlo manages Tarcan; ben is Makinabang staff.
        foreign_po = Procurement.objects.exclude(branch__branch_name='Tarcan').first()
        self.assertEqual(
            self.login('carlo').get(f'/procurement/{foreign_po.pk}/', {'modal': '1'}).status_code,
            302,
        )
        tarcan_order = SalesTransaction.objects.filter(
            branch__branch_name='Tarcan'
        ).first()
        self.assertEqual(
            self.login('ben').get(f'/sales/{tarcan_order.pk}/', {'modal': '1'}).status_code,
            302,
        )

    def test_every_view_action_opens_a_modal(self):
        """One list left behind is one module that still navigates away.

        The eye button is the same markup in every module, so this finds them
        by it rather than by a list of files that a new module would not be
        on.

        Financials is the deliberate exception: the Order Console and Billing
        list navigate to Order Detail / Billing Detail as full pages, not a
        modal — those detail pages link onward to each other (Order Detail
        <-> Billing Detail) and to their own print page, which reads as
        actual navigation with a real back button rather than a quick
        look-up over the list. See templates/sales/transaction_list.html and
        templates/billing/billing_list.html.
        """
        EXEMPT_VIEW_ACTIONS = {
            'sales/transaction_list.html',
            'billing/billing_list.html',
        }
        offenders = []
        for root in settings.TEMPLATES[0]['DIRS']:
            for path in Path(root).rglob('*.html'):
                rel = str(path.relative_to(root)).replace('\\', '/')
                if rel in EXEMPT_VIEW_ACTIONS:
                    continue
                text = path.read_text(encoding='utf-8')
                for anchor in re.finditer(r'<a\s[^>]*?>', text, re.S):
                    tag = anchor.group(0)
                    if 'title="View"' in tag and 'data-detail-modal' not in tag:
                        offenders.append(rel)

        self.assertEqual(
            offenders, [],
            'These "see" actions still navigate to a page of their own. Add '
            'data-detail-modal="<record noun>" to the link, and make sure the '
            'detail template it points at extends base_template:\n  '
            + '\n  '.join(offenders),
        )


class FloorStockScopeBarTests(TestCase):
    """The branch scope bar on Floor Stock.

    The bar is a UI convenience sitting on top of scope_queryset(), and the
    danger with any such control is that it becomes the only thing standing
    between an account and another branch's data. These tests keep the two
    separable: the picker is only rendered for an account that may actually
    use it, and hand-typing ?branch= past it returns nothing.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    def login(self, username):
        client = Client()
        client.force_login(User.objects.get(username=username))
        return client

    def test_owner_gets_a_segment_for_every_branch_plus_all(self):
        html = self.login('gerlyn').get('/inventory/stock/').content.decode()
        self.assertIn('class="seg"', html)
        self.assertIn('All Branches', html)
        for branch in Branch.objects.all():
            self.assertIn(branch.branch_name, html)

    def test_manager_gets_no_picker_only_their_branch(self):
        """A Manager must not be shown a control listing branches they are
        not allowed to open — the request would come back empty and read as
        a bug rather than as a permission boundary."""
        carlo = User.objects.get(username='carlo')
        html = self.login('carlo').get('/inventory/stock/').content.decode()

        self.assertNotIn('class="seg"', html)
        self.assertIn('locked-branch', html)
        self.assertIn(carlo.manager_profile.branch.branch_name, html)
        for branch in Branch.objects.exclude(pk=carlo.manager_profile.branch_id):
            self.assertNotIn(branch.branch_name, html)

    def test_manager_typing_another_branch_id_gets_no_rows(self):
        """The bar is not the security boundary. scope_queryset() is."""
        carlo = User.objects.get(username='carlo')
        foreign = Branch.objects.exclude(pk=carlo.manager_profile.branch_id).first()
        self.assertIsNotNone(foreign, 'seed data must contain a second branch')
        self.assertGreater(
            Inventory.objects.filter(branch=foreign).count(), 0,
            'the foreign branch needs stock, or this test passes vacuously',
        )

        response = self.login('carlo').get(f'/inventory/stock/?branch={foreign.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['rows']), 0)

    def test_owner_filtering_to_a_branch_returns_only_that_branch(self):
        branch = Branch.objects.get(branch_name='Tarcan')
        response = self.login('gerlyn').get(f'/inventory/stock/?branch={branch.pk}')
        rows = response.context['rows']

        self.assertGreater(len(rows), 0)
        self.assertTrue(all(r['inv'].branch_id == branch.pk for r in rows))
        # The Branch column is redundant once every row is one branch.
        self.assertFalse(response.context['show_branch_column'])

    def test_segment_counts_are_per_branch_not_the_whole_table(self):
        segments = self.login('gerlyn').get('/inventory/stock/').context['scope_branches']
        by_name = {s['name']: s for s in segments}

        self.assertEqual(
            by_name['All Branches']['total'], Inventory.objects.count(),
        )
        for branch in Branch.objects.all():
            self.assertEqual(
                by_name[branch.branch_name]['total'],
                Inventory.objects.filter(branch=branch).count(),
                f'{branch.branch_name} segment must count only its own rows',
            )

    def test_segment_links_carry_the_other_filters_across(self):
        """Switching branch must not silently clear the search someone typed."""
        segments = (
            self.login('gerlyn')
            .get('/inventory/stock/?q=tuna&status=In+Stock')
            .context['scope_branches']
        )
        tarcan = next(s for s in segments if s['name'] == 'Tarcan')

        self.assertIn('q=tuna', tarcan['href'])
        self.assertIn('status=In+Stock', tarcan['href'])
        self.assertIn(f'branch={Branch.objects.get(branch_name="Tarcan").pk}', tarcan['href'])

    def test_unassigned_manager_sees_neither_picker_nor_rows(self):
        """NULL profile branch: no picker (that would be the fail-open bug in
        UI form), no rows, and an explanation instead of a blank page."""
        carlo = User.objects.get(username='carlo')
        carlo.manager_profile.branch = None
        carlo.manager_profile.save(update_fields=['branch'])

        response = self.login('carlo').get('/inventory/stock/')
        html = response.content.decode()

        self.assertNotIn('class="seg"', html)
        self.assertEqual(len(response.context['rows']), 0)
        self.assertIn('No branch is assigned to your account', html)
