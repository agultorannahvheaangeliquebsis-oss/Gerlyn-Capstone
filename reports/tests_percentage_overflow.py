"""Regression tests for percentages that overflow their element or lie.

KPI delta pills were already capped and zero-baseline-safe (a46a19f). What was
left: bars and meters spending an unclamped ``pct`` as a CSS width, the Billing
Report's "% of Revenue" going above 100% or negative once refunds exceed a
payment method's sales, and donut shares going negative for a net-negative row.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase

from accounts.models import User
from reports.views import (
    _bar_width_pct,
    _billing_payment_breakdown,
    _donut,
    _inventory_value_chart,
)

RAMP = ['#111', '#222', '#333']


def _record(method, amount, txn_id):
    return SimpleNamespace(
        payment_method=method, amount_due=Decimal(amount), transaction_id=txn_id,
    )


class BarWidthClampTests(SimpleTestCase):
    def test_in_range_values_pass_through(self):
        self.assertEqual(_bar_width_pct(0), 0.0)
        self.assertEqual(_bar_width_pct(42.5), 42.5)
        self.assertEqual(_bar_width_pct(100), 100.0)

    def test_overflow_and_negative_are_clamped(self):
        self.assertEqual(_bar_width_pct(130.4), 100.0)
        self.assertEqual(_bar_width_pct(-25), 0.0)

    def test_non_numbers_draw_an_empty_bar(self):
        for bad in (None, '', 'abc', float('nan'), float('inf'), float('-inf')):
            with self.subTest(bad=bad):
                self.assertEqual(_bar_width_pct(bad), 0.0)


class InventoryValueChartTests(SimpleTestCase):
    def test_bars_carry_a_clamped_width_beside_the_real_pct(self):
        chart = _inventory_value_chart([
            {'name': 'A', 'value': 900},
            {'name': 'B', 'value': 300},
        ])
        for bar in chart['bars']:
            self.assertGreaterEqual(bar['width_pct'], 0)
            self.assertLessEqual(bar['width_pct'], 100)
            self.assertEqual(bar['width_pct'], _bar_width_pct(bar['pct']))

    def test_negative_value_draws_an_empty_bar_but_keeps_its_pct(self):
        chart = _inventory_value_chart([
            {'name': 'A', 'value': 800},
            {'name': 'B', 'value': -200},
        ])
        negative = chart['bars'][1]
        self.assertLess(negative['pct'], 0)
        self.assertEqual(negative['width_pct'], 0.0)

    def test_all_negative_series_draws_nothing_instead_of_crashing(self):
        chart = _inventory_value_chart([{'name': 'A', 'value': -50}])
        self.assertEqual(chart, {'bars': [], 'ticks': []})


class DonutShareTests(SimpleTestCase):
    def test_positive_rows_sum_to_one_hundred(self):
        chart = _donut([('A', 600), ('B', 300), ('C', 100)], RAMP, 'Other')
        self.assertAlmostEqual(sum(s['pct'] for s in chart['segments']), 100, delta=0.2)

    def test_net_negative_row_gets_zero_share_and_others_stay_in_range(self):
        chart = _donut([('A', 1500), ('B', 500), ('C', -500)], RAMP, 'Other')
        pcts = {s['name']: s['pct'] for s in chart['segments']}
        self.assertEqual(pcts['C'], 0.0)
        self.assertAlmostEqual(pcts['A'], 75.0, places=1)
        self.assertAlmostEqual(pcts['B'], 25.0, places=1)
        for pct in pcts.values():
            self.assertTrue(0 <= pct <= 100)
        # The hole still prints the real, signed total.
        self.assertEqual(chart['total'], 1500)

    def test_negative_row_inside_the_folded_group_keeps_wedges_at_one_hundred(self):
        rows = [('A', 500), ('B', 400), ('C', 300), ('D', -100)]
        chart = _donut(rows, RAMP, 'Other')
        self.assertEqual(chart['overflow'], 2)
        self.assertAlmostEqual(sum(s['pct'] for s in chart['segments']), 100, delta=0.2)
        for segment in chart['segments']:
            self.assertTrue(0 <= segment['pct'] <= 100)

    def test_nothing_positive_to_draw_returns_the_empty_chart(self):
        chart = _donut([('A', -100), ('B', -50)], RAMP, 'Other')
        self.assertEqual(chart['segments'], [])
        self.assertEqual(chart['total'], 0)

    def test_arc_lengths_are_never_negative(self):
        chart = _donut([('A', 1000), ('B', -400)], RAMP, 'Other')
        for segment in chart['segments']:
            dash, _rest = (float(x) for x in segment['dasharray'].split())
            self.assertGreater(dash, 0)


class BillingPaymentBreakdownTests(SimpleTestCase):
    def test_shares_are_real_percentages_when_everything_is_positive(self):
        rows = _billing_payment_breakdown(
            [_record('Cash', '750', 1), _record('GCash', '250', 2)], Decimal('1000'),
        )
        pcts = {r['method']: r['pct'] for r in rows}
        self.assertEqual(pcts, {'Cash': 75.0, 'GCash': 25.0})

    def test_refund_that_makes_a_method_negative_hides_every_share(self):
        # Cash nets +1500, GCash nets -500: against a 1000 total these would
        # read 150% and -50%.
        rows = _billing_payment_breakdown(
            [_record('Cash', '1500', 1), _record('GCash', '100', 2)],
            Decimal('1000'),
            refunds_by_transaction={2: Decimal('600')},
        )
        self.assertEqual({r['method']: r['total'] for r in rows},
                         {'Cash': Decimal('1500'), 'GCash': Decimal('-500')})
        self.assertTrue(all(r['pct'] is None for r in rows))

    def test_zero_total_revenue_has_no_shares(self):
        rows = _billing_payment_breakdown([_record('Cash', '100', 1)], Decimal('0'),
                                          refunds_by_transaction={1: Decimal('100')})
        self.assertTrue(all(r['pct'] is None for r in rows))

    def test_negative_total_revenue_has_no_shares(self):
        rows = _billing_payment_breakdown([_record('Cash', '100', 1)], Decimal('-40'))
        self.assertTrue(all(r['pct'] is None for r in rows))

    def test_tiny_positive_total_cannot_blow_a_share_past_one_hundred(self):
        # One method nets -990 against +1000, so total_revenue is a tiny 10:
        # the positive method would be 10000% of it without the guard.
        rows = _billing_payment_breakdown(
            [_record('Cash', '1000', 1), _record('GCash', '10', 2)],
            Decimal('10'),
            refunds_by_transaction={2: Decimal('1000')},
        )
        self.assertTrue(all(r['pct'] is None for r in rows))

    def test_none_total_revenue_is_safe(self):
        rows = _billing_payment_breakdown([_record('Cash', '100', 1)], None)
        self.assertTrue(all(r['pct'] is None for r in rows))


class PercentageRenderingTests(TestCase):
    """The templates must use the clamped width and print a dash for None."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='pct-overflow-owner', password='pw', role=User.Role.OWNER
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.owner)

    def test_billing_report_shows_a_dash_not_a_percentage_for_none(self):
        rows = [{'method': 'Cash', 'count': 1, 'total': Decimal('1500'), 'pct': None}]
        with mock.patch('reports.views._billing_payment_breakdown', return_value=rows):
            html = self.client.get('/reports/billing/').content.decode()
        self.assertIn('data-label="% of Revenue" class="num gv-meta">&mdash;</td>', html)
        self.assertNotIn('None%', html)

    def test_billing_report_still_prints_real_percentages(self):
        rows = [{'method': 'Cash', 'count': 1, 'total': Decimal('1500'), 'pct': 62.5}]
        with mock.patch('reports.views._billing_payment_breakdown', return_value=rows):
            html = self.client.get('/reports/billing/').content.decode()
        self.assertIn('class="num gv-meta">62.5%</td>', html)

    def test_inventory_report_bar_width_uses_the_clamped_value(self):
        chart = {
            'bars': [{'name': 'Main', 'value': 5, 'pct': 130.0, 'width_pct': 100.0}],
            'ticks': ['0', '5'],
        }
        with mock.patch('reports.views._inventory_value_chart', return_value=chart):
            html = self.client.get('/reports/inventory/').content.decode()
        self.assertIn('style="width:100.0%;"', html)
        self.assertNotIn('width:130', html)
