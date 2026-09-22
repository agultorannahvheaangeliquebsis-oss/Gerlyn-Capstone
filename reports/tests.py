from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from inventory.models import Batch, Inventory, Product, ProductCategory
from procurement.models import Procurement
from sales.models import SalesItem, SalesTransaction
from reports.views import (
    NON_PERFORMING_SALES_WINDOW_DAYS,
    _dashboard_branch_breakdown,
    _dashboard_daily_series,
    _dashboard_range_totals,
    _non_performing_products,
    _previous_window,
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
