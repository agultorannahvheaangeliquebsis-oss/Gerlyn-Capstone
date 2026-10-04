"""Dashboard KPI graph ("Last 7 days / Last 30 days — overlaid with the N days before").

Layout contract, every range: one day = one equal step from the plot's left
edge to its right edge, whatever month a day falls in. 'Last 7 days' labels
every day (weekday, centred under its point); longer ranges label each
month at its first day (labels only — they never move a point). The curve is
monotone: every segment is a curve and none leaves the band its two end
values span.
"""
import re
from datetime import date, timedelta

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User
from reports.views import (
    _even_x_fractions, _kpi_axis_labels_month_starts, _smooth_curve_commands,
)

PAD_X, WIDTH = 24, 760


class EvenFractionsTests(SimpleTestCase):
    def test_first_flush_left_last_flush_right_equal_steps(self):
        fractions = _even_x_fractions(7)
        self.assertEqual(fractions[0], 0.0)
        self.assertEqual(fractions[-1], 1.0)
        steps = {round(b - a, 9) for a, b in zip(fractions, fractions[1:])}
        self.assertEqual(len(steps), 1)

    def test_single_point_sits_in_the_middle(self):
        self.assertEqual(_even_x_fractions(1), [0.5])

    def test_no_points(self):
        self.assertEqual(_even_x_fractions(0), [])


class SevenDayGraphLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='kg-owner', password='pw', role=User.Role.OWNER,
        )

    def graphs(self, **params):
        client = Client()
        client.force_login(self.owner)
        r = client.get(reverse('accounts:owner_dashboard'), {'range': '7', **params})
        return r.context['kpi_graphs']

    def test_every_metric_has_seven_centred_evenly_spaced_ticks(self):
        for key, graph in self.graphs().items():
            if key == 'avg_transaction_value' and not graph['axisLabels']:
                continue
            ticks = graph['axisLabels']
            with self.subTest(metric=key):
                self.assertEqual(len(ticks), 7)
                self.assertEqual({t['anchor'] for t in ticks}, {'middle'})
                self.assertEqual(ticks[0]['x'], PAD_X)
                self.assertEqual(ticks[-1]['x'], WIDTH - PAD_X)
                gaps = {round(b['x'] - a['x'], 1) for a, b in zip(ticks, ticks[1:])}
                self.assertLessEqual(max(gaps) - min(gaps), 0.2)   # rounding only

    def test_total_sales_points_sit_under_their_ticks(self):
        graph = self.graphs()['total_sales']
        self.assertEqual(
            [p['x'] for p in graph['points']], [t['x'] for t in graph['axisLabels']],
        )

    def test_longer_ranges_get_month_ticks_not_one_per_day(self):
        ticks = self.graphs(range='30')['total_sales']['axisLabels']
        self.assertLessEqual(len(ticks), 3)


def _segments(cmds):
    """[(cp1y, cp2y, endx, endy)] for each 'C' segment of a curve."""
    out = []
    for m in re.finditer(r'C([\d.]+),([\d.]+) ([\d.]+),([\d.]+) ([\d.]+),([\d.]+)', cmds):
        out.append((float(m.group(2)), float(m.group(4)), float(m.group(5)), float(m.group(6))))
    return out


class MonotoneCurveTests(SimpleTestCase):
    BASE = 196.0   # a plot baseline (y grows downward)

    def curve(self, ys, xs=None):
        xs = xs or [24 + i * 24 for i in range(len(ys))]
        coords = list(zip(xs, ys))
        return coords, _smooth_curve_commands(coords, 24, self.BASE)

    def test_every_segment_is_a_curve_never_a_straight_line(self):
        _, cmds = self.curve([196, 196, 60, 196, 196, 30, 120, 196])
        self.assertNotIn('L', cmds)
        self.assertEqual(cmds.count('C'), 7)

    def test_control_points_stay_between_their_segments_endpoints(self):
        ys = [196, 196, 196, 40, 196, 196, 150, 24, 196, 196]
        coords, cmds = self.curve(ys)
        for i, (cp1y, cp2y, _x, endy) in enumerate(_segments(cmds)):
            lo, hi = sorted((coords[i][1], endy))
            self.assertTrue(lo <= cp1y <= hi, (i, cp1y, lo, hi))
            self.assertTrue(lo <= cp2y <= hi, (i, cp2y, lo, hi))

    def test_a_flat_run_of_zero_days_stays_exactly_on_the_baseline(self):
        _, cmds = self.curve([196, 196, 196, 196, 40, 196])
        for cp1y, cp2y, _x, endy in _segments(cmds)[:2]:
            self.assertEqual((cp1y, cp2y, endy), (self.BASE, self.BASE, self.BASE))

    def test_wide_x_gaps_do_not_fall_back_to_straight_lines(self):
        # 29 evenly spaced points then one far away (the old >4x-median rule).
        xs = [24 + i * 10 for i in range(29)] + [736]
        _, cmds = self.curve([196] * 14 + [60, 90] + [196] * 14, xs)
        self.assertNotIn('L', cmds)

    def test_two_points_are_a_straight_line(self):
        self.assertEqual(_smooth_curve_commands([(24, 196), (736, 40)]), 'L736,40')


class MonthStartTickTests(SimpleTestCase):
    def labels(self, start, days):
        return [(start + timedelta(n)).isoformat() for n in range(days)]

    def test_ticks_sit_at_each_months_first_day_and_never_move_points(self):
        days = self.labels(date(2026, 9, 5), 30)             # Sep 5 .. Oct 4
        fractions = _even_x_fractions(30)
        ticks = _kpi_axis_labels_month_starts(days, fractions)
        self.assertEqual([t['text'] for t in ticks], ['SEPT', 'OCT'])
        self.assertEqual(ticks[0]['x'], 24.0)                # range's first day, flush left
        oct_first = days.index('2026-10-01')
        self.assertEqual(ticks[1]['x'], round(24 + fractions[oct_first] * 712, 1))
        self.assertEqual(ticks[0]['anchor'], 'start')
        self.assertEqual(ticks[1]['anchor'], 'middle')

    def test_spacing_is_one_equal_step_per_day_across_a_month_boundary(self):
        fractions = _even_x_fractions(30)
        steps = {round(b - a, 9) for a, b in zip(fractions, fractions[1:])}
        self.assertEqual(len(steps), 1)
        self.assertEqual(fractions[-1], 1.0)

    def test_many_months_are_thinned_to_the_label_cap(self):
        days = self.labels(date(2025, 1, 1), 700)
        ticks = _kpi_axis_labels_month_starts(days, _even_x_fractions(700))
        self.assertLessEqual(len(ticks), 7)

    def test_single_day_range_label_is_centred(self):
        (tick,) = _kpi_axis_labels_month_starts(['2026-10-04'], _even_x_fractions(1))
        self.assertEqual(tick['anchor'], 'middle')


class ThirtyDayGraphLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='kg30-owner', password='pw', role=User.Role.OWNER,
        )

    def graphs(self):
        client = Client()
        client.force_login(self.owner)
        r = client.get(reverse('accounts:owner_dashboard'), {'range': '30'})
        return r.context['kpi_graphs']

    def test_points_fill_the_width_with_equal_steps_whatever_the_month(self):
        for key in ('total_sales', 'procurement_cost', 'total_transactions'):
            xs = [p['x'] for p in self.graphs()[key]['points']]
            with self.subTest(metric=key):
                self.assertEqual(len(xs), 30)
                self.assertEqual(xs[0], PAD_X)
                self.assertEqual(xs[-1], WIDTH - PAD_X)
                gaps = {round(b - a, 1) for a, b in zip(xs, xs[1:])}
                self.assertLessEqual(max(gaps) - min(gaps), 0.2)

    def test_month_ticks_sit_under_a_month_first_day_point(self):
        graph = self.graphs()['total_sales']
        by_x = {p['x']: p['date'] for p in graph['points']}
        ticks = graph['axisLabels']
        self.assertGreaterEqual(len(ticks), 1)
        for i, tick in enumerate(ticks):
            day = by_x[tick['x']]
            with self.subTest(tick=tick['text']):
                self.assertTrue(day.endswith('-01') or tick['x'] == PAD_X, day)
