from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from procurement.models import Procurement
from sales.models import SalesTransaction
from reports.views import (
    _dashboard_branch_breakdown,
    _dashboard_daily_series,
    _dashboard_range_totals,
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
