"""
Acceptance tests for the five end-to-end scenarios in the build plan.

Run with:
    python manage.py test core

These are the checks the ISO 25010 evaluation leans on for Functional
Suitability, Reliability, and Security — so they must stay green.
"""
from django.core.management import call_command
from django.test import Client, TestCase

from accounts.models import User
from branches.models import Branch
from core.scoping import scope_queryset
from inventory.models import (
    Inventory, Product, StockMovement, StockTransfer, StockTransferItem, Warehouse,
)
from procurement.models import Procurement
from sales.models import SalesTransaction


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
        before = inv.quantity_on_hand

        customer = self.login('juan')
        session = customer.session
        session['branch_id'] = branch.id
        session['cart'] = {str(inv.product_id): 2}
        session.save()
        customer.post('/shop/checkout/', {
            'mode_of_payment': 'Cash on Delivery',
            'delivery_address': 'Baliuag',
        })

        order = SalesTransaction.objects.filter(customer=juan).order_by('-id').first()
        inv.refresh_from_db()

        self.assertEqual(order.order_status, 'Pending')
        self.assertTrue(hasattr(order, 'billing_record'), 'exactly one billing record')
        self.assertEqual(inv.quantity_on_hand, before, 'stock must not move at checkout')

        staff = self.login('ana')
        for status in ['Approved', 'Preparing', 'Completed']:
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
        po = Procurement.objects.filter(status='Ordered').first()
        items = list(po.items.all())
        inv, _ = Inventory.objects.get_or_create(
            product=items[0].product, branch=po.branch,
            defaults={'quantity_on_hand': 0, 'reorder_level': 0},
        )
        before = inv.quantity_on_hand

        self.login('gerlyn').post(f'/procurement/deliveries/{po.pk}/receive/', {
            'item_id': [str(i.pk) for i in items],
            'quantity_received': [str(i.quantity_ordered - 5) for i in items],
        })

        po.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(po.receipt_flag, 'Discrepancy')
        self.assertEqual(inv.quantity_on_hand, before + items[0].quantity_ordered - 5)
        self.assertTrue(
            StockMovement.objects.filter(
                reference_id=po.pk, movement_type='Procurement Received'
            ).exists(),
            'receipt must be audited',
        )

    def test_double_receive_is_blocked(self):
        po = Procurement.objects.filter(status='Ordered').first()
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
                    '/inventory/alerts/', '/manage/users/']:
            self.assertEqual(
                client.get(url).status_code, 302,
                f'a supplier must not reach {url}',
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

    def test_supplier_can_confirm_delivery_date_on_own_order(self):
        supplier = User.objects.get(username='delmonte')
        po = Procurement.objects.filter(supplier=supplier, status='Ordered').first()
        client = Client()
        client.force_login(supplier)
        client.post(f'/procurement/portal/{po.pk}/delivery-date/',
                    {'delivery_date': '2026-08-01'})
        po.refresh_from_db()
        self.assertEqual(str(po.delivery_date), '2026-08-01')

    def test_supplier_cannot_mark_their_own_order_received(self):
        """Business rule 13: only the store validates a receipt."""
        supplier = User.objects.get(username='delmonte')
        po = Procurement.objects.filter(supplier=supplier, status='Ordered').first()
        client = Client()
        client.force_login(supplier)
        client.post(f'/procurement/{po.pk}/update-status/', {'status': 'Received'})
        po.refresh_from_db()
        self.assertEqual(po.status, 'Ordered')

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

    # --- wholesale pricing --------------------------------------------------
    # Chapter 1 describes Gerlyn as a "retail and wholesale business".

    def test_retail_sale_uses_retail_price(self):
        product = Product.objects.get(sku_code='SKU-005')   # 48 retail / 41 wholesale
        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash', 'order_type': 'Retail',
            'product_id': [str(product.pk)], 'quantity': ['10'],
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.order_type, 'Retail')
        self.assertEqual(tx.items.first().unit_price, product.unit_price)

    def test_wholesale_sale_uses_wholesale_price(self):
        product = Product.objects.get(sku_code='SKU-005')
        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash', 'order_type': 'Wholesale',
            'product_id': [str(product.pk)], 'quantity': ['10'],
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.order_type, 'Wholesale')
        self.assertEqual(tx.items.first().unit_price, product.wholesale_price)
        self.assertEqual(tx.total_amount, product.wholesale_price * 10)

    def test_wholesale_falls_back_to_retail_when_no_wholesale_price(self):
        """Never sell at ₱0 just because a product has no wholesale price."""
        product = Product.objects.get(sku_code='SKU-005')
        Product.objects.filter(pk=product.pk).update(wholesale_price=None)
        product.refresh_from_db()

        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash', 'order_type': 'Wholesale',
            'product_id': [str(product.pk)], 'quantity': ['2'],
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.total_amount, product.unit_price * 2)

    def test_invalid_order_type_falls_back_to_retail(self):
        product = Product.objects.get(sku_code='SKU-005')
        self.login('ana').post('/sales/add/', {
            'mode_of_payment': 'Cash', 'order_type': 'Free',
            'product_id': [str(product.pk)], 'quantity': ['1'],
        })
        tx = SalesTransaction.objects.order_by('-id').first()
        self.assertEqual(tx.order_type, 'Retail')

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
        html = client.get(f'/inventory/products/edit/{product.pk}/').content.decode()
        self.assertIn('value="1345.50"', html, 'number input must not be localised')
        self.assertNotIn('value="1,345.50"', html)

    def test_csv_export_is_branch_scoped(self):
        owner_rows = self.login('gerlyn').get('/reports/export/sales.csv').content.count(b'\n')
        manager_rows = self.login('carlo').get('/reports/export/sales.csv').content.count(b'\n')
        self.assertGreater(owner_rows, manager_rows)

    def test_staff_cannot_export_reports(self):
        response = self.login('ana').get('/reports/export/sales.csv')
        self.assertNotIn('text/csv', response['Content-Type'])
