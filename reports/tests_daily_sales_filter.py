"""P13 — daily sales date filter: single day, date range and the Today /
Yesterday / Last 7 days presets, on the Sales Report page, its JSON mirror
and its CSV export.

What these pin down:
  * the day boundaries — local (Asia/Manila) 00:00:00 through 23:59:59.999999
    inclusive, judged on aware datetimes so a row that is "yesterday" in UTC
    but "today" in Manila lands on Manila's day;
  * the page, the API and the Reports hub print the same totals for the same
    params (they all read reports.services);
  * Decimal money, Completed-only revenue, refunds netted out;
  * branch scoping: a manager never leaves their branch, an unassigned one
    sees nothing, an Owner may narrow with ?branch=;
  * a range the request names but that cannot be honoured is a 400 on the API
    (and a warning on the page), never a silently different period.
"""
import csv
import io
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from core.timebounds import (
    DATE_RANGE_SESSION_KEY, _explicit_range_params, range_from_request,
    range_problem, resolve_date_range,
)
from sales.models import Refund, SalesTransaction

DAY = date(2026, 3, 10)
SALES_API = '/api/reports/sales-report/'


def _local(day, hour=0, minute=0, second=0, microsecond=0):
    return timezone.make_aware(
        datetime(day.year, day.month, day.day, hour, minute, second, microsecond)
    )


class _Request:
    """Just enough request for range_from_request: GET params + a session."""

    def __init__(self, **params):
        self.GET = params
        self.session = {}


class ResolveDateRangeTests(SimpleTestCase):
    TODAY = date(2026, 9, 25)

    def resolve(self, preset, date_from=None, date_to=None):
        return resolve_date_range(preset, date_from, date_to, today=self.TODAY)

    def test_today_and_yesterday_are_single_days(self):
        self.assertEqual(self.resolve('today'), (self.TODAY, self.TODAY, 'today'))
        y = self.TODAY - timedelta(days=1)
        self.assertEqual(self.resolve('yesterday'), (y, y, 'yesterday'))

    def test_last_7_days_is_seven_calendar_days_inclusive_of_today(self):
        start, end, preset = self.resolve('7')
        self.assertEqual((end - start).days, 6)
        self.assertEqual((end, preset), (self.TODAY, '7'))

    def test_day_uses_the_given_date_for_both_ends(self):
        self.assertEqual(self.resolve('day', DAY), (DAY, DAY, 'day'))

    def test_day_without_a_date_falls_back_to_30_days(self):
        self.assertEqual(self.resolve('day')[2], '30')

    def test_custom_rejects_inverted_and_missing_bounds(self):
        self.assertEqual(self.resolve('custom', DAY, DAY - timedelta(days=1))[2], '30')
        self.assertEqual(self.resolve('custom', DAY, None)[2], '30')


class ExplicitRangeParamTests(SimpleTestCase):
    def parse(self, **params):
        return _explicit_range_params(params)

    def test_a_bare_date_implies_a_single_day(self):
        preset, date_from, _to, problem = self.parse(date='2026-03-10')
        self.assertEqual((preset, date_from, problem), ('day', DAY, ''))

    def test_bare_from_and_to_imply_a_custom_range(self):
        preset, date_from, date_to, problem = self.parse(
            date_from='2026-03-01', date_to='2026-03-10',
        )
        self.assertEqual(
            (preset, date_from, date_to, problem), ('custom', date(2026, 3, 1), DAY, ''),
        )

    def test_nothing_named_is_not_a_problem(self):
        self.assertEqual(self.parse(), ('', None, None, ''))

    def test_problems(self):
        for params in (
            {'range': 'fortnight'},
            {'range': 'day'},
            {'range': 'day', 'date': 'not-a-date'},
            {'date': '2026-13-45'},
            {'range': 'custom', 'date_from': '2026-03-01'},
            {'range': 'custom', 'date_from': 'x', 'date_to': '2026-03-01'},
            {'range': 'custom', 'date_from': '2026-03-10', 'date_to': '2026-03-01'},
            {'date_from': '2026-03-10'},
        ):
            with self.subTest(params=params):
                self.assertTrue(self.parse(**params)[3])

    def test_params_the_preset_does_not_use_are_ignored(self):
        # The shared control submits date_from/date_to/date with EVERY preset
        # click, so junk in them must not break `range=today`.
        for preset in ('today', 'yesterday', '7', '30', 'all'):
            with self.subTest(preset=preset):
                problem = self.parse(
                    range=preset, date='garbage', date_from='x', date_to='y',
                )[3]
                self.assertEqual(problem, '')

    def test_day_accepts_date_from_for_the_audit_forms_hidden_inputs(self):
        preset, date_from, _to, problem = self.parse(
            range='day', date_from='2026-03-10', date_to='2026-03-10',
        )
        self.assertEqual((preset, date_from, problem), ('day', DAY, ''))


class RangeFromRequestTests(SimpleTestCase):
    def test_single_day_is_stored_and_restored_from_the_session(self):
        first = _Request(range='day', date='2026-03-10')
        self.assertEqual(range_from_request(first), (DAY, DAY, 'day'))
        self.assertEqual(first.session[DATE_RANGE_SESSION_KEY]['range'], 'day')

        follow_up = _Request()
        follow_up.session = first.session
        self.assertEqual(range_from_request(follow_up), (DAY, DAY, 'day'))

    def test_yesterday_rolls_with_the_calendar_rather_than_pinning(self):
        request = _Request(range='yesterday')
        start, end, preset = range_from_request(request, today=date(2026, 3, 11))
        self.assertEqual((start, end, preset), (DAY, DAY, 'yesterday'))
        follow_up = _Request()
        follow_up.session = request.session
        # A day later, "yesterday" is a different date.
        self.assertEqual(
            range_from_request(follow_up, today=date(2026, 3, 12))[0], date(2026, 3, 11),
        )

    def test_range_problem_reads_the_request(self):
        self.assertEqual(range_problem(_Request(range='today')), '')
        self.assertTrue(range_problem(_Request(range='day')))


class DailySalesFixture(TestCase):
    """One branch of dated sales spanning the boundaries of DAY."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            username='ds-owner', password='pw', role=User.Role.OWNER,
        )
        cls.branch = Branch.objects.create(
            branch_name='DS Branch A', location='Baliuag', owner=cls.owner,
        )
        cls.other_branch = Branch.objects.create(
            branch_name='DS Branch B', location='Plaridel', owner=cls.owner,
        )
        cls.manager = cls._manager('ds-manager', cls.branch)

        # --- Branch A ---------------------------------------------------
        cls.before = cls._sale('10.05', _local(DAY - timedelta(days=1), 23, 59, 59))
        cls.day_start = cls._sale('100.10', _local(DAY, 0, 0, 0))
        # 16:30 UTC on the 9th is 00:30 on the 10th in Manila: it is DAY's.
        cls.utc_prev_evening = cls._sale(
            '5.05', datetime(2026, 3, 9, 16, 30, tzinfo=dt_timezone.utc),
        )
        cls.day_noon = cls._sale('200.20', _local(DAY, 12, 0, 0))
        cls.day_end = cls._sale('50.05', _local(DAY, 23, 59, 59, 999999))
        cls.after = cls._sale('1000.00', _local(DAY + timedelta(days=1), 0, 0, 0))
        # Same day, but not revenue: listed, never summed into sales.
        cls.cancelled = cls._sale(
            '700.00', _local(DAY, 9, 0), SalesTransaction.OrderStatus.CANCELLED,
        )
        cls.pending = cls._sale(
            '400.00', _local(DAY, 10, 0), SalesTransaction.OrderStatus.PENDING,
        )
        Refund.objects.create(
            transaction=cls.day_noon, reason='DS refund',
            refund_method=Refund.RefundMethod.CASH,
            total_amount=Decimal('20.20'), refunded_by=cls.owner,
        )

        # --- Branch B, same day ------------------------------------------
        cls.b_sale = cls._sale('300.00', _local(DAY, 11, 0), branch=cls.other_branch)

    @classmethod
    def _manager(cls, username, branch):
        user = User.objects.create_user(
            username=username, password='pw', role=User.Role.BRANCH_MANAGER,
        )
        profile = user.manager_profile
        profile.branch = branch
        profile.save(update_fields=['branch'])
        return user

    @classmethod
    def _sale(cls, amount, when, order_status=None, branch=None):
        txn = SalesTransaction.objects.create(
            branch=branch or cls.branch, total_amount=Decimal(amount),
            transaction_type=SalesTransaction.TransactionType.PHYSICAL,
            mode_of_payment=SalesTransaction.ModeOfPayment.CASH,
        )
        # transaction_date is auto_now_add; .update() is the only way to place it.
        SalesTransaction.objects.filter(pk=txn.pk).update(transaction_date=when)
        # New sales are not Completed on creation; revenue only counts once they are.
        order_status = order_status or SalesTransaction.OrderStatus.COMPLETED
        if txn.order_status != order_status:
            SalesTransaction.objects.filter(pk=txn.pk).update(order_status=order_status)
        txn.refresh_from_db()
        return txn

    def login(self, user=None):
        client = Client()
        client.force_login(user or self.owner)
        return client

    def api(self, params, user=None):
        return self.login(user).get(SALES_API, params)

    @staticmethod
    def money(value):
        return Decimal(str(value))


class SingleDayBoundaryTests(DailySalesFixture):
    # Branch A, DAY: day_start 100.10 + utc_prev_evening 5.05 + day_noon 200.20
    # + day_end 50.05 = 355.40 gross, over 4 Completed orders. cancelled and
    # pending sit on the same day and must not move that figure.
    GROSS = Decimal('355.40')
    REFUNDS = Decimal('20.20')
    NET = Decimal('335.20')

    def in_day(self):
        return {
            self.day_start.pk, self.utc_prev_evening.pk, self.day_noon.pk,
            self.day_end.pk, self.cancelled.pk, self.pending.pk,
        }

    def test_a_single_day_includes_both_edges_and_excludes_the_neighbours(self):
        body = self.api({'range': 'day', 'date': DAY.isoformat(), 'branch': self.branch.pk}).json()
        listed = {row['id'] for row in body['results']}

        self.assertEqual(listed, self.in_day())
        self.assertNotIn(self.before.pk, listed)   # 23:59:59 the day before
        self.assertNotIn(self.after.pk, listed)    # 00:00:00 the day after
        self.assertNotIn(self.b_sale.pk, listed)   # another branch

    def test_utc_evening_belongs_to_the_next_manila_day(self):
        prev = self.api(
            {'date': (DAY - timedelta(days=1)).isoformat(), 'branch': self.branch.pk}
        ).json()
        self.assertNotIn(self.utc_prev_evening.pk, {r['id'] for r in prev['results']})

    def test_api_totals_are_completed_only_decimal_and_net_of_refunds(self):
        totals = self.api({'date': DAY.isoformat(), 'branch': self.branch.pk}).json()['totals']

        self.assertEqual(self.money(totals['grossSales']), self.GROSS)
        self.assertEqual(self.money(totals['refundValue']), self.REFUNDS)
        self.assertEqual(self.money(totals['netSales']), self.NET)
        self.assertEqual(totals['orders'], 4)
        self.assertEqual(totals['transactions'], 6)          # + cancelled + pending
        self.assertEqual(self.money(totals['avgOrderValue']), Decimal('88.85'))
        self.assertEqual(self.money(totals['pipelineValue']), Decimal('400.00'))
        self.assertEqual(totals['pipelineCount'], 1)

    def test_bare_date_means_a_single_day_on_the_api(self):
        body = self.api({'date': DAY.isoformat(), 'branch': self.branch.pk}).json()
        self.assertEqual(body['activeRange'], 'day')
        self.assertEqual((body['rangeStart'], body['rangeEnd']), (DAY.isoformat(),) * 2)

    def test_page_and_api_print_the_same_totals(self):
        params = {'range': 'day', 'date': DAY.isoformat(), 'branch': self.branch.pk}
        page = self.login().get('/reports/sales/', params)
        api = self.api(params).json()['totals']
        ctx = page.context['totals']

        self.assertEqual(ctx['gross_sales'], self.money(api['grossSales']))
        self.assertEqual(ctx['refund_value'], self.money(api['refundValue']))
        self.assertEqual(ctx['net_sales'], self.money(api['netSales']))
        self.assertEqual(ctx['avg_order_value'], self.money(api['avgOrderValue']))
        self.assertEqual((ctx['orders'], ctx['transactions']), (api['orders'], api['transactions']))
        self.assertEqual({t.pk for t in page.context['transactions']}, self.in_day())
        # What is printed is what is summed.
        self.assertContains(page, '₱335.20')

    def test_reports_hub_net_sales_matches_the_daily_sales_totals(self):
        params = {'range': 'day', 'date': DAY.isoformat(), 'branch': self.branch.pk}
        hub = self.login().get('/reports/', params).context
        api = self.api(params).json()['totals']

        self.assertEqual(hub['total_sales'], self.money(api['netSales']))
        self.assertEqual(hub['gross_sales'], self.money(api['grossSales']))
        self.assertEqual(hub['total_orders'], api['orders'])
        self.assertEqual(hub['pipeline_total'], self.money(api['pipelineValue']))

    def test_dashboard_summary_api_agrees_on_the_same_day(self):
        params = {'range': 'day', 'date': DAY.isoformat(), 'branch': self.branch.pk}
        summary = self.login().get('/api/reports/dashboard-summary/', params).json()
        totals = self.api(params).json()['totals']

        self.assertEqual(self.money(summary['netSales']), self.money(totals['netSales']))
        self.assertEqual(self.money(summary['grossSales']), self.money(totals['grossSales']))

    def test_owner_without_a_branch_sees_every_branch(self):
        totals = self.api({'date': DAY.isoformat()}).json()['totals']
        self.assertEqual(self.money(totals['grossSales']), self.GROSS + Decimal('300.00'))

    def test_single_day_survives_in_the_session_for_the_next_page_load(self):
        client = self.login()
        client.get('/reports/sales/', {'range': 'day', 'date': DAY.isoformat()})
        ctx = client.get('/reports/sales/').context
        self.assertEqual((ctx['active_range'], ctx['range_end']), ('day', DAY))


class DateRangeTests(DailySalesFixture):
    def test_custom_range_is_inclusive_of_both_end_days(self):
        body = self.api({
            'range': 'custom', 'date_from': (DAY - timedelta(days=1)).isoformat(),
            'date_to': DAY.isoformat(), 'branch': self.branch.pk,
        }).json()
        listed = {row['id'] for row in body['results']}

        self.assertIn(self.before.pk, listed)        # 23:59:59 on date_from
        self.assertIn(self.day_end.pk, listed)       # 23:59:59.999999 on date_to
        self.assertNotIn(self.after.pk, listed)      # 00:00:00 the day after date_to
        self.assertEqual(
            self.money(body['totals']['grossSales']), Decimal('355.40') + Decimal('10.05'),
        )

    def test_bare_from_and_to_imply_custom_on_the_api(self):
        body = self.api({
            'date_from': DAY.isoformat(), 'date_to': DAY.isoformat(), 'branch': self.branch.pk,
        }).json()
        self.assertEqual(body['activeRange'], 'custom')
        self.assertEqual(body['totals']['orders'], 4)


class PresetTests(DailySalesFixture):
    """Today / Yesterday / Last 7 days, against the real local clock."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = timezone.localdate()
        cls.today = today
        cls.yesterday_start = cls._sale('11.00', _local(today - timedelta(days=1), 0, 0, 0))
        cls.yesterday_end = cls._sale('12.00', _local(today - timedelta(days=1), 23, 59, 59))
        cls.today_start = cls._sale('13.00', _local(today, 0, 0, 0))
        cls.six_days_ago = cls._sale('14.00', _local(today - timedelta(days=6), 0, 0, 0))
        cls.seven_days_end = cls._sale('15.00', _local(today - timedelta(days=7), 23, 59, 59))

    def ids(self, preset):
        body = self.api({'range': preset, 'branch': self.branch.pk}).json()
        return {row['id'] for row in body['results']}, body

    def test_yesterday_is_exactly_yesterdays_local_day(self):
        ids, body = self.ids('yesterday')
        self.assertEqual(ids, {self.yesterday_start.pk, self.yesterday_end.pk})
        self.assertEqual(self.money(body['totals']['grossSales']), Decimal('23.00'))
        self.assertEqual(body['rangeStart'], (self.today - timedelta(days=1)).isoformat())

    def test_today_excludes_yesterdays_last_second(self):
        ids, _ = self.ids('today')
        self.assertIn(self.today_start.pk, ids)
        self.assertNotIn(self.yesterday_end.pk, ids)

    def test_last_7_days_starts_at_midnight_six_days_ago(self):
        ids, _ = self.ids('7')
        self.assertIn(self.six_days_ago.pk, ids)
        self.assertNotIn(self.seven_days_end.pk, ids)
        self.assertTrue({self.yesterday_start.pk, self.today_start.pk} <= ids)

    def test_page_offers_the_new_presets(self):
        page = self.login().get('/reports/sales/', {'range': 'yesterday'})
        self.assertEqual(page.context['active_range'], 'yesterday')
        self.assertContains(page, 'value="yesterday"')
        self.assertContains(page, 'name="date"')   # the single-day input


class BadRangeTests(DailySalesFixture):
    BAD = (
        {'range': 'fortnight'},
        {'range': 'day'},
        {'range': 'day', 'date': 'nope'},
        {'date': '2026-02-30'},
        {'range': 'custom', 'date_from': DAY.isoformat()},
        {'range': 'custom', 'date_from': '2026-03-10', 'date_to': '2026-03-01'},
    )

    def test_sales_report_api_answers_400_rather_than_a_different_period(self):
        for params in self.BAD:
            with self.subTest(params=params):
                response = self.api(params)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()['detail'])

    def test_dashboard_summary_api_answers_400_too(self):
        for params in self.BAD:
            with self.subTest(params=params):
                response = self.login().get('/api/reports/dashboard-summary/', params)
                self.assertEqual(response.status_code, 400)

    def test_page_warns_and_falls_back_to_30_days(self):
        page = self.login().get('/reports/sales/', {'range': 'day'})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['active_range'], '30')
        texts = [str(m) for m in page.context['messages']]
        self.assertTrue(any('last 30 days' in t for t in texts), texts)

    def test_the_shared_controls_own_submission_is_not_a_bad_range(self):
        # Every preset click also sends the custom + single-day inputs.
        response = self.api({
            'range': 'yesterday', 'date': '', 'date_from': '', 'date_to': '',
        })
        self.assertEqual(response.status_code, 200)


class BranchScopingTests(DailySalesFixture):
    def test_manager_is_pinned_to_their_branch_even_when_asking_for_another(self):
        body = self.api(
            {'date': DAY.isoformat(), 'branch': self.other_branch.pk}, user=self.manager,
        ).json()
        ids = {row['id'] for row in body['results']}
        self.assertNotIn(self.b_sale.pk, ids)
        self.assertEqual(ids, {
            self.day_start.pk, self.utc_prev_evening.pk, self.day_noon.pk,
            self.day_end.pk, self.cancelled.pk, self.pending.pk,
        })
        self.assertEqual(self.money(body['totals']['grossSales']), Decimal('355.40'))

    def test_manager_page_totals_stay_in_branch(self):
        page = self.login(self.manager).get(
            '/reports/sales/', {'date': DAY.isoformat(), 'branch': self.other_branch.pk},
        )
        self.assertEqual(page.context['totals']['gross_sales'], Decimal('355.40'))
        self.assertNotIn(self.b_sale.pk, {t.pk for t in page.context['transactions']})

    def test_unassigned_manager_sees_nothing_and_zero_totals(self):
        lonely = self._manager('ds-lonely', self.branch)
        profile = lonely.manager_profile
        profile.branch = None
        profile.save(update_fields=['branch'])

        body = self.api({'date': DAY.isoformat()}, user=lonely).json()
        self.assertEqual(body['results'], [])
        self.assertEqual(body['count'], 0)
        self.assertEqual(self.money(body['totals']['grossSales']), Decimal('0'))

        page = self.login(lonely).get('/reports/sales/', {'date': DAY.isoformat()})
        self.assertEqual(list(page.context['transactions']), [])

    def test_anonymous_api_request_is_refused(self):
        self.assertIn(Client().get(SALES_API, {'date': DAY.isoformat()}).status_code, (401, 403))


class SalesCsvRangeTests(DailySalesFixture):
    def rows(self, params, user=None):
        response = self.login(user).get(reverse('export_csv', args=['sales']), params)
        return response, list(csv.reader(io.StringIO(response.content.decode())))

    def test_csv_with_a_range_holds_only_that_days_rows(self):
        response, rows = self.rows({'range': 'day', 'date': DAY.isoformat(), 'branch': self.branch.pk})
        order_numbers = {row[0] for row in rows[1:]}
        self.assertEqual(response.status_code, 200)
        self.assertEqual(order_numbers, {
            t.order_number for t in (
                self.day_start, self.utc_prev_evening, self.day_noon, self.day_end,
                self.cancelled, self.pending,
            )
        })

    def test_csv_without_a_range_is_still_the_full_export(self):
        _, rows = self.rows({})
        self.assertEqual(len(rows) - 1, SalesTransaction.objects.count())

    def test_csv_with_a_bad_range_redirects_with_a_message(self):
        response, _ = self.rows({'range': 'day'})
        self.assertEqual(response.status_code, 302)

    def test_manager_csv_stays_in_branch(self):
        _, rows = self.rows({'date': DAY.isoformat(), 'branch': self.other_branch.pk}, self.manager)
        self.assertNotIn(self.b_sale.order_number, {row[0] for row in rows[1:]})
        self.assertGreater(len(rows), 1)
