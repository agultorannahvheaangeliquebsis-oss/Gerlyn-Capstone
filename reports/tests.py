from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from inventory.models import Product, ProductCategory
from procurement.models import Procurement
from sales.models import SalesItem, SalesTransaction
from reports.views import (
    _dashboard_branch_breakdown,
    _dashboard_daily_series,
    _dashboard_range_totals,
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
