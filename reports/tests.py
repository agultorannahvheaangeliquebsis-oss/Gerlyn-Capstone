from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from inventory.models import Batch, Inventory, Product, ProductCategory
from procurement.models import Procurement
from sales.models import Refund, SalesItem, SalesTransaction
from reports.views import (
    NON_PERFORMING_SALES_WINDOW_DAYS,
    _dashboard_branch_breakdown,
    _dashboard_daily_series,
    _dashboard_range_totals,
    _non_performing_products,
    _previous_window,
    _sales_by_category_chart,
    UNCATEGORIZED_LABEL,
)


class RevenueExcludesCancelledAndPendingTests(TestCase):
    """F-03 — the Reports dashboard must count only realised revenue.

    Before this, every Sum('total_amount') in reports/views.py ran over every
    transaction regardless of order_status, so a voided (Cancelled) sale and a
    still-Pending online order both inflated Total Sales, the daily trend, the
    branch donut and the average transaction value — while the Billing Report
    on the same data already scoped its revenue to Completed only, so the two
    reports contradicted each other.

    Amounts are distinct and non-overlapping so any leak is unambiguous:
      Completed  1500   <- the only figure that is revenue
      Cancelled   700   <- voided, must not count anywhere
      Pending     400   <- committed pipeline, its own figure only
    """

    COMPLETED = 1500
    CANCELLED = 700
    PENDING = 400

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='f03-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='F03 Branch', location='Baliuag', owner=cls.owner
        )
        # One of each lifecycle state that matters here. total_amount is set at
        # creation and never touched again, so the auto-created BillingRecord's
        # amount_due stays equal to it — which is what lets the Reports total
        # and the Billing total be compared directly.
        cls.completed = cls._sale(cls.COMPLETED, SalesTransaction.OrderStatus.COMPLETED)
        cls.cancelled = cls._sale(cls.CANCELLED, SalesTransaction.OrderStatus.CANCELLED)
        cls.pending = cls._sale(cls.PENDING, SalesTransaction.OrderStatus.PENDING)

    @classmethod
    def _sale(cls, amount, status):
        txn = SalesTransaction.objects.create(
            branch=cls.branch,
            total_amount=amount,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        if txn.order_status != status:
            txn.order_status = status
            txn.save(update_fields=['order_status'])
        return txn

    def _client(self):
        client = Client()
        client.force_login(self.owner)
        return client

    # --- the query layer, exercised directly -----------------------------------

    def test_daily_series_and_range_totals_count_completed_only(self):
        today = timezone.localdate()
        start = today - timedelta(days=29)
        daily = _dashboard_daily_series(
            SalesTransaction.objects.all(), Procurement.objects.none(), start, today
        )
        totals = _dashboard_range_totals(daily)

        self.assertEqual(totals['sales'], self.COMPLETED)
        self.assertEqual(totals['txn_count'], 1)
        # ATV = sales / txn_count — only meaningful once the numerator and
        # denominator both exclude the voided and pending rows.
        self.assertEqual(totals['atv'], self.COMPLETED)

    def test_branch_breakdown_counts_completed_only(self):
        today = timezone.localdate()
        start = today - timedelta(days=29)
        prev_start, prev_end, _ = _previous_window(start, today)
        breakdown, _prev = _dashboard_branch_breakdown(
            SalesTransaction.objects.all(), start, today, prev_start, prev_end
        )
        self.assertEqual(breakdown['total'], self.COMPLETED)
        self.assertEqual(
            [seg['value'] for seg in breakdown['segments']], [self.COMPLETED]
        )

    # --- the rendered dashboard ---------------------------------------------

    def test_dashboard_kpis_exclude_cancelled_and_pending(self):
        resp = self._client().get('/reports/')
        ctx = resp.context

        self.assertEqual(ctx['total_sales'], self.COMPLETED)
        self.assertEqual(ctx['total_orders'], 1)
        self.assertEqual(ctx['avg_order_value'], self.COMPLETED)
        self.assertEqual(ctx['branch_chart']['total'], self.COMPLETED)
        # Daily Sales panel peak is the one place the series' magnitude is
        # printed; a leak of the 700 or 400 row (same day, same bucket) would
        # push it to 2600.
        self.assertIn(f'₱{self.COMPLETED:,.2f}', ctx['sales_series']['peak'])

    def test_pipeline_figure_reflects_pending_only(self):
        ctx = self._client().get('/reports/').context
        self.assertEqual(ctx['pipeline_total'], self.PENDING)
        self.assertEqual(ctx['pipeline_count'], 1)

    def test_cancelled_sale_still_listed_in_the_transactions_table(self):
        # Excluded from the money figures, not hidden from the audit trail.
        ctx = self._client().get('/reports/').context
        listed = {t.pk for t in ctx['transactions']}
        self.assertEqual(
            listed, {self.completed.pk, self.cancelled.pk, self.pending.pk}
        )

    # --- the two reports must finally agree --------------------------------

    def test_dashboard_total_sales_matches_billing_report_revenue(self):
        client = self._client()
        dash = client.get('/reports/').context['total_sales']
        billing = client.get('/reports/billing/').context['total_revenue']

        self.assertEqual(dash, self.COMPLETED)
        self.assertEqual(billing, self.COMPLETED)
        self.assertEqual(dash, billing)


class WebApiReportingParityTests(TestCase):
    """Unify Reporting and Net Revenue (P1) — reports/views.py and
    reports/api_views.py both read reports/services.py now instead of each
    keeping its own copy of "what counts as revenue" / "what counts as
    procurement cost" / "what counts as low stock". Before this:

      - The API summed total_amount over every order_status (Pending and
        Cancelled included) where the web Reports tab already summed
        Completed only.
      - Neither side subtracted recorded refunds from a Completed order's
        total — a refund never changes order_status (sales.models.Refund),
        so a refunded order stayed counted at its full original amount.
      - The API's low-stock count read the stored Inventory.restock flag,
        which is only ever recomputed inside Inventory.save() and so can go
        stale; the web side (as of this fix) and the API both now read the
        live quantity_on_hand <= reorder_level predicate instead.
      - The API's procurement cost summed every status, Cancelled included.

    These tests build one fixture with a Completed order that is partially
    refunded, a Cancelled order, a Pending order, purchase orders in
    Received/Approved/Cancelled status, and one Inventory row whose stored
    restock flag is deliberately wrong — then assert the web view and the
    API return the exact same numbers for it.
    """

    COMPLETED = Decimal('1500.00')
    REFUNDED_AWAY = Decimal('200.00')       # part of COMPLETED, refunded back
    CANCELLED_SALE = Decimal('700.00')      # must never count anywhere
    PENDING = Decimal('400.00')             # pipeline only
    RECEIVED_PO = Decimal('1000.00')
    APPROVED_PO = Decimal('300.00')         # committed, not yet received
    CANCELLED_PO = Decimal('9999.00')       # must never count — the "inappropriate status"

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='parity-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Parity Branch', location='Baliuag', owner=cls.owner
        )

        cls.completed = cls._sale(cls.COMPLETED, SalesTransaction.OrderStatus.COMPLETED)
        cls._sale(cls.CANCELLED_SALE, SalesTransaction.OrderStatus.CANCELLED)
        cls._sale(cls.PENDING, SalesTransaction.OrderStatus.PENDING)

        # order_status is deliberately left Completed — a refund never
        # changes it (see sales.models.Refund's own docstring).
        Refund.objects.create(
            transaction=cls.completed, reason='Parity test refund',
            refund_method=Refund.RefundMethod.CASH,
            total_amount=cls.REFUNDED_AWAY, refunded_by=cls.owner,
        )

        today = timezone.localdate()
        Procurement.objects.create(
            branch=cls.branch, order_date=today,
            total_cost=cls.RECEIVED_PO, status=Procurement.Status.RECEIVED,
        )
        Procurement.objects.create(
            branch=cls.branch, order_date=today,
            total_cost=cls.APPROVED_PO, status=Procurement.Status.APPROVED,
        )
        Procurement.objects.create(
            branch=cls.branch, order_date=today,
            total_cost=cls.CANCELLED_PO, status=Procurement.Status.CANCELLED,
        )

        category = ProductCategory.objects.create(category_name='Parity Category')
        product = Product.objects.create(
            category=category, sku_code='SKU-PARITY-001',
            product_name='Parity Product', unit_price=10,
        )
        low = Inventory.objects.create(
            branch=cls.branch, product=product,
            quantity_on_hand=2, reorder_level=10,
        )
        # Force the stored flag stale/wrong via .update() (bypasses save(),
        # which would otherwise recompute it to True) — proves the low-stock
        # figure below reads the live predicate, not this flag.
        Inventory.objects.filter(pk=low.pk).update(restock=False)

    @classmethod
    def _sale(cls, amount, order_status):
        txn = SalesTransaction.objects.create(
            branch=cls.branch, total_amount=amount,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        if txn.order_status != order_status:
            txn.order_status = order_status
            txn.save(update_fields=['order_status'])
        return txn

    def _client(self):
        client = Client()
        client.force_login(self.owner)
        return client

    def test_gross_refund_net_and_pipeline_match_between_web_and_api(self):
        client = self._client()
        web = client.get('/reports/').context
        api = client.get('/api/reports/dashboard-summary/').json()

        expected_net = self.COMPLETED - self.REFUNDED_AWAY

        self.assertEqual(web['gross_sales'], self.COMPLETED)
        self.assertEqual(web['refund_value'], self.REFUNDED_AWAY)
        self.assertEqual(web['total_sales'], expected_net)
        self.assertEqual(web['pipeline_total'], self.PENDING)

        self.assertEqual(Decimal(str(api['grossSales'])), self.COMPLETED)
        self.assertEqual(Decimal(str(api['refundValue'])), self.REFUNDED_AWAY)
        self.assertEqual(Decimal(str(api['netSales'])), expected_net)
        self.assertEqual(Decimal(str(api['totalSales'])), expected_net)
        self.assertEqual(Decimal(str(api['pipelineValue'])), self.PENDING)

        # The web/API pair, read side by side.
        self.assertEqual(web['gross_sales'], Decimal(str(api['grossSales'])))
        self.assertEqual(web['refund_value'], Decimal(str(api['refundValue'])))
        self.assertEqual(web['total_sales'], Decimal(str(api['netSales'])))
        self.assertEqual(web['pipeline_total'], Decimal(str(api['pipelineValue'])))

    def test_procurement_cost_excludes_cancelled_and_matches_procurement_report(self):
        client = self._client()
        api = client.get('/api/reports/dashboard-summary/').json()
        procurement_ctx = client.get('/reports/procurement/').context

        expected = self.RECEIVED_PO + self.APPROVED_PO  # CANCELLED_PO excluded
        web_total = procurement_ctx['received_value'] + procurement_ctx['active_value']

        self.assertEqual(web_total, expected)
        self.assertEqual(Decimal(str(api['totalProcurementCost'])), expected)
        self.assertEqual(Decimal(str(api['totalProcurementCost'])), web_total)

    def test_low_stock_count_reads_the_live_predicate_not_the_stale_flag(self):
        client = self._client()
        api = client.get('/api/reports/dashboard-summary/').json()
        inventory_ctx = client.get('/reports/inventory/').context
        dashboard_ctx = client.get(reverse('accounts:owner_dashboard')).context

        self.assertEqual(api['lowStockCount'], 1)
        self.assertEqual(inventory_ctx['low_critical_count'], 1)
        self.assertEqual(dashboard_ctx['inventory_low_critical'], 1)
        self.assertEqual(api['lowStockCount'], inventory_ctx['low_critical_count'])


class BillingReportRowsLinkToBillingDetailTests(TestCase):
    """The Billing Report used to duplicate the Billing list's table but drop
    the Action column, leaving no way to open a record from the report. Each
    row must now link to the same billing_detail page the Billing list uses.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='billing-report-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Billing Report Branch', location='Baliuag', owner=cls.owner
        )
        cls.txn = SalesTransaction.objects.create(
            branch=cls.branch,
            total_amount=500,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )

    def test_report_row_links_to_the_billing_detail_url(self):
        client = Client()
        client.force_login(self.owner)
        resp = client.get('/reports/billing/')
        record = resp.context['records'][0]

        self.assertIn(f'/billing/{record.pk}/', resp.content.decode())


class SalesByCategoryChartTests(TestCase):
    """Panel-review ask: a meaningful category donut on the Reports Sales tab,
    with the period and percentage basis it covers stated on the page, not
    just implied.

    Mirrors the branch-donut coverage above: the aggregation must (a) group
    revenue by category, (b) fold every line item whose product has no
    category (or whose product is gone) into one Uncategorized bucket rather
    than dropping it, and (c) — via the rendered view — only ever see the
    same Completed-only rows the rest of the Sales tab already agrees on
    (F-03), so a cancelled sale's items cannot inflate it.
    """

    GRAINS_REVENUE = 1000    # 20 x P50
    BEVERAGES_REVENUE = 480  # 12 x P40
    UNCATEGORIZED_REVENUE = 200  # 10 x P20
    TOTAL = GRAINS_REVENUE + BEVERAGES_REVENUE + UNCATEGORIZED_REVENUE

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='cat-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Category Branch', location='Baliuag', owner=cls.owner
        )
        cls.grains = ProductCategory.objects.create(category_name='Grains')
        cls.beverages = ProductCategory.objects.create(category_name='Beverages')

        cls.rice = Product.objects.create(
            category=cls.grains, sku_code='SKU-CAT-001', product_name='Rice',
            unit_price=50,
        )
        cls.soda = Product.objects.create(
            category=cls.beverages, sku_code='SKU-CAT-002', product_name='Soda',
            unit_price=40,
        )
        cls.misc = Product.objects.create(
            category=None, sku_code='SKU-CAT-003', product_name='Misc',
            unit_price=20,
        )

        cls.completed = cls._sale(SalesTransaction.OrderStatus.COMPLETED)
        cls._item(cls.completed, cls.rice, quantity=20, unit_price=50)
        cls._item(cls.completed, cls.soda, quantity=12, unit_price=40)
        cls._item(cls.completed, cls.misc, quantity=10, unit_price=20)

        # A cancelled sale's items must never reach the chart — hugely
        # outweighs the completed rows so any leak is unmistakable.
        cls.cancelled = cls._sale(SalesTransaction.OrderStatus.CANCELLED)
        cls._item(cls.cancelled, cls.rice, quantity=999, unit_price=50)

    @classmethod
    def _sale(cls, status):
        txn = SalesTransaction.objects.create(
            branch=cls.branch,
            total_amount=0,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        if txn.order_status != status:
            txn.order_status = status
            txn.save(update_fields=['order_status'])
        return txn

    @classmethod
    def _item(cls, txn, product, quantity, unit_price):
        return SalesItem.objects.create(
            transaction=txn, product=product,
            quantity_sold=quantity, unit_price=unit_price,
        )

    def test_groups_by_category_and_folds_no_category_lines_into_uncategorized(self):
        chart = _sales_by_category_chart(
            SalesTransaction.objects.filter(pk=self.completed.pk)
        )

        self.assertEqual(chart['total'], self.TOTAL)
        by_name = {seg['name']: seg['value'] for seg in chart['segments']}
        self.assertEqual(by_name, {
            'Grains': self.GRAINS_REVENUE,
            'Beverages': self.BEVERAGES_REVENUE,
            UNCATEGORIZED_LABEL: self.UNCATEGORIZED_REVENUE,
        })
        # Percentages are each segment's share of the same stated total — they
        # must sum back to (approximately) it, modulo the one-decimal rounding
        # _donut prints.
        self.assertAlmostEqual(
            sum(seg['pct'] for seg in chart['segments']), 100.0, delta=0.2
        )
        self.assertEqual(chart['category_count'], 3)

    def test_rendered_report_excludes_cancelled_sale_items(self):
        client = Client()
        client.force_login(self.owner)
        ctx = client.get('/reports/').context

        self.assertEqual(ctx['category_chart']['total'], self.TOTAL)
        by_name = {seg['name']: seg['value'] for seg in ctx['category_chart']['segments']}
        # The cancelled sale's 999 x P50 Rice line would dwarf and swallow
        # every other segment if it leaked in.
        self.assertEqual(by_name.get('Grains'), self.GRAINS_REVENUE)


class NonPerformingProductsReportTests(TestCase):
    """Panel review flag (Category DSS/Reporting): "Create a table with
    product/SKU, branch, stock, expiry, recent sales/velocity, and status.
    Define 'non-performing' using a documented rule."

    Exercises reports.views._non_performing_products() — the rule itself is
    documented in docs/METRICS.md. Six products, one branch, cover every
    combination the Status column can show:

      slow_seller     stock, zero Completed sales in the window -> Non-Performing only
      fast_seller     stock, sold within the window                -> not flagged at all
      cancelled_only  stock, sales exist but only Cancelled/Pending -> still Non-Performing
                       (a non-Completed order is not real velocity)
      ageing_seller   stock, sold within the window, batch Expiring Soon -> Near-Expiry only
      dead_and_ageing stock, zero sales, batch Expiring Soon        -> both flags (danger)
      out_of_stock    zero stock, zero sales                        -> excluded (that's
                       low_stock_alerts()'s job, not this report's)
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='nonperf-owner', password='pw', role=User.Role.OWNER
        )
        cls.branch = Branch.objects.create(
            branch_name='Non-Perf Branch', location='Baliuag', owner=cls.owner
        )
        cls.other_branch = Branch.objects.create(
            branch_name='Other Branch', location='San Rafael', owner=cls.owner
        )
        category = ProductCategory.objects.create(category_name='Dry Goods')

        def make_product(sku):
            return Product.objects.create(
                category=category, sku_code=sku, product_name=f'Product {sku}',
                unit_price=50,
            )

        cls.slow_seller = make_product('NP-SLOW')
        cls.fast_seller = make_product('NP-FAST')
        cls.cancelled_only = make_product('NP-CANCELLED')
        cls.ageing_seller = make_product('NP-AGEING')
        cls.dead_and_ageing = make_product('NP-DEAD-AGEING')
        cls.out_of_stock = make_product('NP-OOS')

        for product, qty in [
            (cls.slow_seller, 10), (cls.fast_seller, 10),
            (cls.cancelled_only, 10), (cls.ageing_seller, 10),
            (cls.dead_and_ageing, 10), (cls.out_of_stock, 0),
        ]:
            Inventory.objects.create(
                product=product, branch=cls.branch,
                quantity_on_hand=qty, reorder_level=2,
            )

        # A branch-scoping control row: plenty of stock, no sales at all —
        # would read as Non-Performing if branch scoping leaked, but lives
        # on a different branch than every assertion below scopes to.
        cls.other_branch_product = make_product('NP-OTHER-BRANCH')
        Inventory.objects.create(
            product=cls.other_branch_product, branch=cls.other_branch,
            quantity_on_hand=10, reorder_level=2,
        )

        recent = timezone.now() - timedelta(days=5)
        stale = timezone.now() - timedelta(days=NON_PERFORMING_SALES_WINDOW_DAYS + 5)

        cls._sale(cls.fast_seller, cls.branch, recent, SalesTransaction.OrderStatus.COMPLETED)
        cls._sale(cls.ageing_seller, cls.branch, recent, SalesTransaction.OrderStatus.COMPLETED)
        # Sold, but outside the window and/or not Completed — neither counts
        # as real recent velocity, so these products stay Non-Performing.
        cls._sale(cls.slow_seller, cls.branch, stale, SalesTransaction.OrderStatus.COMPLETED)
        cls._sale(cls.cancelled_only, cls.branch, recent, SalesTransaction.OrderStatus.CANCELLED)
        cls._sale(cls.cancelled_only, cls.branch, recent, SalesTransaction.OrderStatus.PENDING)

        today = timezone.localdate()
        cls._batch(cls.ageing_seller, cls.branch, today + timedelta(days=10))
        cls._batch(cls.dead_and_ageing, cls.branch, today + timedelta(days=10))
        # Fresh stock, far outside even the Approaching Expiry tier — must
        # never contribute a Near-Expiry flag.
        cls._batch(cls.fast_seller, cls.branch, today + timedelta(days=200))

    @classmethod
    def _sale(cls, product, branch, when, status):
        txn = SalesTransaction.objects.create(
            branch=branch, total_amount=500,
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        SalesTransaction.objects.filter(pk=txn.pk).update(
            transaction_date=when, order_status=status
        )
        SalesItem.objects.create(
            transaction=txn, product=product, quantity_sold=5, unit_price=50,
        )

    @classmethod
    def _batch(cls, product, branch, expiry_date):
        Batch.objects.create(
            product=product, branch=branch, location=Batch.Location.FLOOR,
            quantity=10, received_date=timezone.localdate(), expiry_date=expiry_date,
        )

    def _rows_by_product(self, branch_ids=None):
        rows = _non_performing_products(branch_ids)
        return {r['product'].pk: r for r in rows}

    def test_slow_seller_flagged_non_performing_only(self):
        rows = self._rows_by_product([self.branch.pk])
        row = rows[self.slow_seller.pk]
        self.assertEqual(row['status'], 'Non-Performing')
        self.assertEqual(row['pill_class'], 'pill-warn')
        self.assertTrue(row['is_non_performing'])
        self.assertFalse(row['is_near_expiry'])
        self.assertEqual(row['units_sold'], 0)

    def test_fast_seller_not_flagged(self):
        rows = self._rows_by_product([self.branch.pk])
        self.assertNotIn(self.fast_seller.pk, rows)

    def test_sale_outside_completed_status_does_not_count_as_velocity(self):
        rows = self._rows_by_product([self.branch.pk])
        row = rows[self.cancelled_only.pk]
        self.assertTrue(row['is_non_performing'])
        self.assertEqual(row['units_sold'], 0)

    def test_ageing_seller_flagged_near_expiry_only(self):
        rows = self._rows_by_product([self.branch.pk])
        row = rows[self.ageing_seller.pk]
        self.assertEqual(row['status'], 'Near-Expiry')
        self.assertEqual(row['pill_class'], 'pill-warn')
        self.assertFalse(row['is_non_performing'])
        self.assertTrue(row['is_near_expiry'])
        self.assertEqual(row['units_sold'], 5)

    def test_dead_and_ageing_flagged_both_with_danger_pill(self):
        rows = self._rows_by_product([self.branch.pk])
        row = rows[self.dead_and_ageing.pk]
        self.assertEqual(row['status'], 'Non-Performing + Near-Expiry')
        self.assertEqual(row['pill_class'], 'pill-danger')
        self.assertTrue(row['is_non_performing'])
        self.assertTrue(row['is_near_expiry'])

    def test_out_of_stock_product_excluded(self):
        rows = self._rows_by_product([self.branch.pk])
        self.assertNotIn(self.out_of_stock.pk, rows)

    def test_other_branch_excluded_by_scoping(self):
        rows = self._rows_by_product([self.branch.pk])
        self.assertNotIn(self.other_branch_product.pk, rows)

    def test_none_branch_ids_returns_every_branch(self):
        rows = _non_performing_products(None)
        pks = {r['product'].pk for r in rows}
        self.assertIn(self.other_branch_product.pk, pks)
        self.assertIn(self.slow_seller.pk, pks)

    def test_worst_rows_sort_first(self):
        rows = _non_performing_products([self.branch.pk])
        statuses = [r['status'] for r in rows]
        rank = {'Non-Performing + Near-Expiry': 0, 'Non-Performing': 1, 'Near-Expiry': 2}
        ranks = [rank[s] for s in statuses]
        self.assertEqual(ranks, sorted(ranks))

    def test_inventory_report_view_renders_the_table(self):
        client = Client()
        client.force_login(self.owner)
        resp = client.get('/reports/inventory/')

        ctx_pks = {r['product'].pk for r in resp.context['non_performing_rows']}
        self.assertIn(self.slow_seller.pk, ctx_pks)
        self.assertIn(self.dead_and_ageing.pk, ctx_pks)
        self.assertNotIn(self.fast_seller.pk, ctx_pks)

        body = resp.content.decode()
        self.assertIn('Non-Performing &amp; Near-Expiry Products', body)
        self.assertIn('Non-Performing + Near-Expiry', body)
