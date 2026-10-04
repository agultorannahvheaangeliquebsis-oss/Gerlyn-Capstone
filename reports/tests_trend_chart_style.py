"""Sales Trend by Branch — visual rendering rules (colours + area fill).

Data and geometry are covered elsewhere; these pin only the styling contract:
every branch line gets its own hue from TREND_BRANCH_COLOURS, the legend and
tooltip use the very same value, and the area fill is drawn only when a single
branch is shown.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from reports.views import TREND_BRANCH_COLOURS, _sales_trend_by_branch_chart
from sales.models import SalesTransaction


class TrendChartStyleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='tc-owner', password='pw', role=User.Role.OWNER,
        )
        cls.branches = [
            Branch.objects.create(branch_name=f'TC {n}', location='Baliuag', owner=cls.owner)
            for n in 'ABCDE'
        ]
        now = timezone.now()
        for i, branch in enumerate(cls.branches):
            txn = SalesTransaction.objects.create(
                branch=branch, total_amount=Decimal('100.00') * (i + 1),
                transaction_type=SalesTransaction.TransactionType.PHYSICAL,
                mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
            )
            SalesTransaction.objects.filter(pk=txn.pk).update(
                transaction_date=now - timedelta(days=1),
                order_status=SalesTransaction.OrderStatus.COMPLETED,
            )

    def chart(self, branches):
        today = timezone.localdate()
        return _sales_trend_by_branch_chart(
            SalesTransaction.objects.all(), branches,
            today - timedelta(days=6), today, active_range='7',
        )

    def test_each_branch_gets_a_distinct_colour_in_order(self):
        lines = self.chart(self.branches[:3])['lines']
        self.assertEqual(
            [line['colour'] for line in lines], TREND_BRANCH_COLOURS[:3],
        )
        self.assertEqual(len({line['colour'] for line in lines}), 3)

    def test_colours_wrap_after_the_set_is_used_up(self):
        lines = self.chart(self.branches * 2)['lines']
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[6]['colour'], TREND_BRANCH_COLOURS[0])

    def test_no_area_fill_with_multiple_branches(self):
        for line in self.chart(self.branches[:3])['lines']:
            self.assertEqual(line['areaPath'], '')

    def test_area_fill_for_a_single_branch_closes_to_the_baseline(self):
        (line,) = self.chart(self.branches[:1])['lines']
        self.assertTrue(line['areaPath'].startswith(line['linePath']))
        self.assertTrue(line['areaPath'].endswith('Z'))

    def test_tooltip_points_use_the_same_colours_as_the_lines(self):
        chart = self.chart(self.branches[:3])
        line_colours = [line['colour'] for line in chart['lines']]
        for point in chart['points']:
            self.assertEqual([v['colour'] for v in point['values']], line_colours)

    def test_page_legend_swatches_match_line_strokes(self):
        client = Client()
        client.force_login(self.owner)
        r = client.get(reverse('accounts:owner_dashboard'), {'range': '7'})
        html = r.content.decode()
        for colour in TREND_BRANCH_COLOURS[:3]:
            self.assertIn(f'stroke="{colour}"', html)
            self.assertIn(f'background:{colour};', html)

    def test_days_are_evenly_spaced_across_the_full_width(self):
        points = self.chart(self.branches[:3])['points']
        xs = [p['x'] for p in points]
        self.assertEqual(len(xs), 7)
        self.assertEqual(xs[0], 24.0)            # flush left (pad_x)
        self.assertEqual(xs[-1], 736.0)          # flush right (width - pad_x)
        gaps = {round(b - a, 1) for a, b in zip(xs, xs[1:])}
        self.assertLessEqual(max(gaps) - min(gaps), 0.2)   # rounding only

    def test_weekday_ticks_sit_under_their_points(self):
        chart = self.chart(self.branches[:3])
        self.assertEqual(len(chart['axisLabels']), 7)
        self.assertEqual(
            [tick['x'] for tick in chart['axisLabels']],
            [p['x'] for p in chart['points']],
        )

    def test_rendered_chart_has_no_dash_arrays(self):
        client = Client()
        client.force_login(self.owner)
        html = client.get(reverse('accounts:owner_dashboard'), {'range': '7'}).content.decode()
        svg = html[html.index('id="trend-svg"'):]
        svg = svg[:svg.index('</svg>')]
        self.assertNotIn('stroke-dasharray', svg)
