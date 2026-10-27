"""Profitability calculations (reports/profitability.py): accuracy with refunds,
missing and estimated costs, branch scoping, and the period rules.

Every expected number comes from the table in reports/testing_profit.py and was
worked out by hand, not read back from the code under test.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from accounts.models import User
from expenses.models import Expense
from inventory.models import Product
from procurement.models import Procurement
from reports import profitability as pf
from reports.testing_profit import D, ProfitFixture, make_user
from sales.models import SalesItem


class WaterfallTests(ProfitFixture):
    def build(self, user=None, **kw):
        return pf.build_profitability(user or self.owner, self.start, self.today, **kw)

    def test_owner_totals_match_the_hand_calculation(self):
        result = self.build()
        self.assert_figures(result.totals, 'all')
        self.assertEqual(result.totals.gross_margin, D('68.3'))     # 1230 / 1800
        self.assertEqual(result.totals.net_margin, D('17.2'))       # 310 / 1800
        self.assertEqual(result.totals.status, 'profit')

    def test_each_branch_row_matches_and_rows_add_up_to_the_total(self):
        result = self.build()
        rows = {f.branch.pk: f for f in result.branches}
        self.assert_figures(rows[self.branch_a.pk], 'a')
        self.assert_figures(rows[self.branch_b.pk], 'b')
        for attr in ('gross_sales', 'refunds', 'cogs', 'expenses', 'net_profit'):
            self.assertEqual(sum(getattr(f, attr) for f in result.branches), getattr(result.totals, attr))

    def test_only_completed_orders_in_range_count(self):
        # The cancelled, pending and 40-day-old orders carry 100 + 100 + 5,000 of
        # sales; none of it may appear.
        self.assertEqual(self.build().totals.gross_sales, D('2100'))

    def test_pending_and_rejected_refunds_move_nothing(self):
        # Branch A has 200 approved plus 100 pending and 100 rejected.
        row = next(f for f in self.build().branches if f.branch == self.branch_a)
        self.assertEqual(row.refunds, D('200'))

    def test_sellable_refund_returns_its_cost_but_a_damaged_one_does_not(self):
        rows = {f.branch.pk: f for f in self.build().branches}
        # A: 10 x 30 + 5 x 10 + 0 - (2 sellable x 30 back on the shelf) = 290
        self.assertEqual(rows[self.branch_a.pk].cogs, D('290'))
        # B: 8 x 35 = 280; the 1 refunded unit was damaged, so its cost stays.
        self.assertEqual(rows[self.branch_b.pk].cogs, D('280'))

    def test_a_frozen_cost_is_not_rewritten_by_a_later_cost_change(self):
        before = self.build().totals.cogs
        Product.objects.filter(pk=self.p1.pk).update(cost_price=D('400'))   # supplier price jumps
        self.assertEqual(self.build().totals.cogs, before)

    def test_expense_dates_are_inclusive_at_the_start_and_exclude_the_day_before(self):
        # Salaries 300 is dated on the first day of the range, Rent 999 the day before.
        row = next(f for f in self.build().branches if f.branch == self.branch_a)
        self.assertEqual(row.expenses, D('800'))

    def test_expenses_by_category_lists_every_category(self):
        by_cat = {c: amount for c, amount, _share in self.build().expenses_by_category}
        self.assertEqual(set(by_cat), set(Expense.Category.values))
        self.assertEqual(by_cat['Rent'], D('500'))
        self.assertEqual(by_cat['Salaries'], D('300'))
        self.assertEqual(by_cat['Utilities'], D('120'))
        self.assertEqual(by_cat['Supplies'], D('0.00'))

    def test_net_profit_is_negative_when_expenses_exceed_gross_profit(self):
        self.expense(self.branch_a, Expense.Category.MAINTENANCE, '5000')
        result = self.build()
        self.assertEqual(result.totals.net_profit, D('310') - D('5000'))
        self.assertEqual(result.totals.status, 'loss')

    def test_no_activity_gives_zero_and_no_margin_rather_than_a_division_error(self):
        empty = pf.build_profitability(self.owner, date(2001, 1, 1), date(2001, 1, 31))
        self.assertEqual(empty.totals.net_sales, D('0'))
        self.assertIsNone(empty.totals.gross_margin)
        self.assertIsNone(empty.totals.net_margin)
        self.assertEqual(empty.totals.status, 'breakeven')
        self.assertFalse(empty.has_data)

    def test_all_time_has_no_lower_bound(self):
        result = pf.build_profitability(self.owner, None, self.today)
        # The 40-day-old order (50 x 100, cost 30) and the day-before rent now count.
        self.assertEqual(result.totals.gross_sales, D('2100') + D('5000'))
        self.assertEqual(result.totals.expenses, D('920') + D('999'))


class ProcurementIsInformationOnlyTests(ProfitFixture):
    def test_buying_stock_does_not_change_profit_but_is_reported(self):
        before = pf.build_profitability(self.owner, self.start, self.today)
        supplier = User.objects.create_user(username='pf-sup', password='pw', role=User.Role.SUPPLIER)
        Procurement.objects.create(
            branch=self.branch_a, supplier=supplier, status=Procurement.Status.RECEIVED,
            total_cost=D('7500'), order_date=self.today,
        )
        after = pf.build_profitability(self.owner, self.start, self.today)
        self.assertEqual(after.totals.net_profit, before.totals.net_profit)
        self.assertEqual(before.procurement_spend, D('0'))
        self.assertEqual(after.procurement_spend, D('7500'))

    def test_a_cancelled_purchase_order_is_not_spend(self):
        supplier = User.objects.create_user(username='pf-sup2', password='pw', role=User.Role.SUPPLIER)
        Procurement.objects.create(
            branch=self.branch_a, supplier=supplier, status=Procurement.Status.CANCELLED,
            total_cost=D('9999'), order_date=self.today,
        )
        self.assertEqual(pf.build_profitability(self.owner, self.start, self.today).procurement_spend, D('0'))


class MissingCostTests(ProfitFixture):
    def test_a_product_with_no_cost_anywhere_is_listed_and_costs_nothing(self):
        result = pf.build_profitability(self.owner, self.start, self.today)
        self.assertEqual([p.sku for p in result.missing_cost], ['PF-3'])
        missing = result.missing_cost[0]
        self.assertEqual((missing.uncosted_units, missing.uncosted_revenue), (4, D('200')))
        self.assertEqual(missing.cogs, D('0'))
        self.assertTrue(result.is_incomplete)
        self.assertEqual(
            pf.data_notes(result)[0], '1 product has no cost price; profit may be overstated.')

    def test_the_note_counts_products_not_lines(self):
        p4 = Product.objects.create(sku_code='PF-4', product_name='Another', unit_price=D('10'))
        self.sale(self.branch_a, [(p4, 1, '10', None), (self.p3, 2, '50', None)])
        notes = pf.data_notes(pf.build_profitability(self.owner, self.start, self.today))
        self.assertEqual(notes[0], '2 products have no cost price; profit may be overstated.')

    def test_a_line_without_a_frozen_cost_is_valued_at_the_products_current_cost(self):
        result = pf.build_profitability(self.owner, self.start, self.today)
        self.assertEqual(result.estimated_lines, 1)                      # the 5 x P2 sale
        p2 = next(p for p in result.products if p.sku == 'PF-2')
        self.assertEqual(p2.cogs, D('50'))                               # 5 x 10
        self.assertEqual(p2.cost_note, 'Estimated cost')
        self.assertIn('valued at the product', pf.data_notes(result)[1])

    def test_setting_the_missing_cost_clears_the_warning(self):
        Product.objects.filter(pk=self.p3.pk).update(cost_price=D('30'))
        result = pf.build_profitability(self.owner, self.start, self.today)
        self.assertFalse(result.is_incomplete)
        self.assertEqual(result.totals.cogs, D('570') + D('120'))        # 4 x 30 now counted

    def test_a_fully_costed_report_has_no_notes(self):
        SalesItem.objects.filter(unit_cost__isnull=True).update(unit_cost=D('1'))
        self.assertEqual(pf.data_notes(pf.build_profitability(self.owner, self.start, self.today)), [])

    def test_uncosted_products_are_kept_out_of_the_ranking(self):
        # Counted as free, the Mystery Tin would top the table with a 100% margin.
        result = pf.build_profitability(self.owner, self.start, self.today)
        ranked = {p.sku for p in result.top_products + result.bottom_products}
        self.assertNotIn('PF-3', ranked)
        self.assertEqual({p.sku for p in result.products}, {'PF-1', 'PF-2', 'PF-3'})

    def test_a_line_from_a_deleted_product_with_no_cost_is_reported(self):
        txn, (item,) = self.sale(self.branch_a, [(self.p2, 3, '20', None)])
        SalesItem.objects.filter(pk=item.pk).update(product=None)
        result = pf.build_profitability(self.owner, self.start, self.today)
        self.assertEqual(result.removed_product_uncosted, 3)
        self.assertEqual(result.missing_product_count, 2)               # PF-3 and the removed product


class ProductRankingTests(ProfitFixture):
    def test_product_profit_is_net_of_refunds_and_restocked_cost(self):
        result = pf.build_profitability(self.owner, self.start, self.today)
        p1 = next(p for p in result.products if p.sku == 'PF-1')
        # sales 1800 - refunds 300 = 1500; cost 300 + 280 - 60 back = 520
        self.assertEqual((p1.net_sales, p1.cogs, p1.gross_profit), (D('1500'), D('520'), D('980')))
        self.assertEqual(p1.margin, D('65.3'))

    def test_product_lines_reconcile_with_net_sales(self):
        result = pf.build_profitability(self.owner, self.start, self.today)
        self.assertEqual(result.unallocated_sales, D('0'))
        self.assertEqual(sum(p.net_sales for p in result.products), result.totals.net_sales)

    def test_top_and_bottom_lists_are_capped_ordered_and_disjoint(self):
        # Fifteen more fully costed products with distinct profits.
        for i in range(15):
            product = Product.objects.create(
                sku_code=f'PF-X{i:02d}', product_name=f'Item {i:02d}', unit_price=D('10'), cost_price=D('1'))
            self.sale(self.branch_a, [(product, 1, str(10 + i), '1')])
        result = pf.build_profitability(self.owner, self.start, self.today)
        self.assertEqual(len(result.top_products), 10)
        self.assertEqual(len(result.bottom_products), 7)                 # 17 costed products - the 10 already shown
        shown = [p.product_id for p in result.top_products]
        self.assertEqual([p.gross_profit for p in result.top_products],
                         sorted((p.gross_profit for p in result.top_products), reverse=True))
        self.assertEqual(result.top_products[0].sku, 'PF-1')
        self.assertFalse(set(shown) & {p.product_id for p in result.bottom_products})
        profits = [p.gross_profit for p in result.bottom_products]
        self.assertEqual(profits, sorted(profits))                       # worst first
        self.assertLessEqual(max(profits), min(p.gross_profit for p in result.top_products))

    def test_a_product_that_lost_money_ranks_last(self):
        loss_leader = Product.objects.create(
            sku_code='PF-LL', product_name='Loss leader', unit_price=D('5'), cost_price=D('9'))
        self.sale(self.branch_a, [(loss_leader, 10, '5', '9')])
        result = pf.build_profitability(self.owner, self.start, self.today)
        # Only three products are fully costed, so all of them fit in the top
        # list (best first) and the loss-making one is its last entry; the
        # "least profitable" list holds only products not already shown above.
        self.assertEqual([p.sku for p in result.top_products], ['PF-1', 'PF-2', 'PF-LL'])
        self.assertEqual(result.top_products[-1].gross_profit, D('-40'))
        self.assertEqual(result.bottom_products, [])


class MonthlyTrendTests(ProfitFixture):
    def test_the_months_add_up_to_the_totals(self):
        result = pf.build_profitability(self.owner, self.start, self.today)
        t = result.totals
        self.assertEqual(sum(m.net_sales for m in result.months), t.net_sales)
        self.assertEqual(sum(m.cogs for m in result.months), t.cogs)
        self.assertEqual(sum(m.expenses for m in result.months), t.expenses)
        self.assertEqual(sum(m.net_profit for m in result.months), t.net_profit)

    def test_a_range_across_a_month_boundary_splits_by_order_date(self):
        # Sales dated on the last day of one month and the first of the next.
        last = date(2026, 1, 31)
        first = date(2026, 2, 1)
        for day, amount in ((last, '100'), (first, '300')):
            when = timezone.make_aware(datetime(day.year, day.month, day.day, 12, 0))
            self.sale(self.branch_a, [(self.p1, 1, amount, '10')], when=when)
        self.expense(self.branch_a, Expense.Category.RENT, '40', when=last)
        self.expense(self.branch_a, Expense.Category.RENT, '60', when=first)
        result = pf.build_profitability(self.owner, last, first)
        jan, feb = result.months
        self.assertEqual((jan.label, feb.label), ('Jan 2026', 'Feb 2026'))
        self.assertEqual((jan.net_sales, jan.cogs, jan.expenses), (D('100'), D('10'), D('40')))
        self.assertEqual((feb.net_sales, feb.cogs, feb.expenses), (D('300'), D('10'), D('60')))
        self.assertTrue(jan.partial and feb.partial)

    def test_only_the_latest_twelve_months_are_charted(self):
        result = pf.build_profitability(self.owner, date(2024, 1, 1), self.today)
        self.assertEqual(len(result.months), pf.MAX_TREND_MONTHS)
        self.assertTrue(result.months_trimmed)
        self.assertEqual(result.months[-1].first, self.today.replace(day=1))


class BranchScopingTests(ProfitFixture):
    def test_a_manager_sees_only_their_branch(self):
        result = pf.build_profitability(self.manager_a, self.start, self.today)
        self.assert_figures(result.totals, 'a')
        self.assertEqual(result.totals.label, 'Profit A')
        self.assertFalse(result.show_comparison)
        self.assertEqual([f.branch for f in result.branches], [self.branch_a])

    def test_a_manager_cannot_widen_the_scope_with_a_branch_argument(self):
        result = pf.build_profitability(
            self.manager_a, self.start, self.today, branch_id=self.branch_b.pk)
        self.assert_figures(result.totals, 'a')
        self.assertEqual({p.sku for p in result.products}, {'PF-1', 'PF-2', 'PF-3'})
        # B's utilities bill (120) is nowhere in A's numbers.
        self.assertEqual(result.totals.expenses, D('800'))

    def test_the_other_managers_branch_is_equally_isolated(self):
        self.assert_figures(pf.build_profitability(self.manager_b, self.start, self.today).totals, 'b')

    def test_a_manager_with_no_branch_sees_nothing(self):
        orphan = make_user('pf-orphan', User.Role.BRANCH_MANAGER)
        result = pf.build_profitability(orphan, self.start, self.today)
        self.assertEqual(result.totals.gross_sales, D('0'))
        self.assertEqual(result.totals.expenses, D('0'))
        self.assertEqual(result.branches, [])
        self.assertEqual(result.products, [])
        self.assertEqual(result.totals.label, 'No branch assigned')

    def test_an_owner_can_narrow_to_one_branch_but_the_comparison_keeps_every_branch(self):
        result = pf.build_profitability(self.owner, self.start, self.today, branch_id=self.branch_b.pk)
        self.assert_figures(result.totals, 'b')
        self.assertEqual(result.totals.label, 'Profit B')
        self.assertTrue(result.show_comparison)
        self.assertEqual({f.branch.pk for f in result.branches}, {self.branch_a.pk, self.branch_b.pk})
        # ...and the narrowed product list is B's alone.
        self.assertEqual([p.sku for p in result.products], ['PF-1'])
        self.assertEqual(result.missing_cost, [])

    def test_a_junk_branch_argument_is_ignored(self):
        result = pf.build_profitability(self.owner, self.start, self.today, branch_id='abc')
        self.assert_figures(result.totals, 'all')

    def test_the_dashboard_figure_is_scoped_the_same_way(self):
        month_start = self.today.replace(day=1)
        owner_total, start, end = pf.month_to_date_net_profit(self.owner, self.today)
        manager_total, _, _ = pf.month_to_date_net_profit(self.manager_a, self.today)
        self.assertEqual((start, end), (month_start, self.today))
        expected_all = pf.build_profitability(self.owner, month_start, self.today).totals
        expected_a = pf.build_profitability(self.manager_a, month_start, self.today).totals
        self.assertEqual(owner_total.net_profit, expected_all.net_profit)
        self.assertEqual(manager_total.net_profit, expected_a.net_profit)
        # B's activity must not leak into the manager's card.
        narrowed, _, _ = pf.month_to_date_net_profit(self.owner, self.today, branch_id=self.branch_a.pk)
        self.assertEqual(narrowed.net_profit, manager_total.net_profit)
