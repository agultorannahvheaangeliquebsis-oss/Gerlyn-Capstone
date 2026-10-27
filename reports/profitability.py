"""
reports/profitability.py -- is the business making money?

One module owns every profit figure, so the Profitability page, its PDF/Excel
export and the Dashboard's Net Profit card cannot disagree. It builds on the
definitions in reports/services.py (what counts as a sale, what a refund is)
and adds cost and expenses.

    Gross sales    Completed orders only, before refunds   (services.gross_sales_total)
  - Refunds        approved refunds against those orders   (services.refund_value_total)
  = Net sales
  - COGS           what the goods sold cost the store
  = Gross profit   (gross margin % = gross profit / net sales)
  - Expenses       rent, utilities, salaries ... recorded in the Expenses module
  = Net profit     (net margin % = net profit / net sales)

Procurement spend is reported beside these as information only. Goods enter the
books through COGS when they are SOLD; subtracting what was bought as well would
count the same stock twice (and make a store that merely stocked up look
unprofitable).

COGS, exactly
-------------
Every sale line carries ``unit_cost`` frozen when its stock left the shelf (see
inventory/costing.py). A line's cost of goods is ``quantity x unit_cost``, rounded
to the centavo per line so every total in this module is a sum of centavo
amounts and the branch rows always add up to the grand total.

  * A line with no frozen cost (recorded before costs were tracked, or its cost
    was unknown at the time) is valued at its product's CURRENT cost price, and
    counted as "estimated" in the completeness note.
  * A line with no cost anywhere contributes 0 and is reported -- profit is
    overstated by exactly that much, and the report says so.
  * A refunded unit put back on the shelf as Sellable was not consumed, so its
    cost comes back out of COGS. A Damaged one is lost stock: its cost stays.

Which date a figure belongs to
------------------------------
Sales, their refunds and their COGS belong to the day the ORDER was placed (the
same rule as every other report: a refund reduces the sale it was against, in
the period of that sale). Expenses belong to the date typed on them.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, Round
from django.utils import timezone

from branches.models import Branch
from core.scoping import branch_ids_for_user
from core.timebounds import local_range_bounds
from expenses.models import Expense
from reports.services import (
    gross_sales_total,
    owner_branch_choice,
    procurement_cost_total,
    procurement_return_totals,
    refund_value_total,
    revenue_sales,
    scoped_procurement_in_range,
    scoped_sales_in_range,
)
from sales.models import Refund, RefundItem, SalesItem

ZERO = Decimal('0.00')
_COST_FIELD = DecimalField(max_digits=18, decimal_places=4)
_MONEY = DecimalField(max_digits=18, decimal_places=2)

# The monthly trend shows the latest twelve months of the range; a chart of
# thirty bars is unreadable, and "All time" can be years.
MAX_TREND_MONTHS = 12
TOP_N = 10


def _cents(value):
    return Decimal(value or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def percent(numerator, denominator):
    """numerator / denominator as a percentage to one decimal place, or None
    when there is nothing to divide by (a margin on zero sales is not 0%)."""
    if not denominator or denominator <= 0:
        return None
    return (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
        Decimal('0.1'), rounding=ROUND_HALF_UP
    )


# --- COGS expressions --------------------------------------------------------

def _unit_cost(prefix=''):
    """The cost to value a sale line at: its frozen cost, else its product's
    current cost price. `prefix` reaches the SalesItem from a RefundItem."""
    return Coalesce(
        F(f'{prefix}unit_cost'), F(f'{prefix}product__cost_price'),
        output_field=_COST_FIELD,
    )


def _line_cogs(prefix='', quantity='quantity_sold'):
    """Cost of goods for one line, rounded to the centavo. NULL (no cost known
    at all) sums as nothing, which is what "overstated" means."""
    return Round(
        ExpressionWrapper(F(quantity) * _unit_cost(prefix), output_field=_COST_FIELD),
        2,
    )


def _uncosted(prefix=''):
    """Lines with no cost anywhere: nothing frozen and no product cost price."""
    return Q(**{f'{prefix}unit_cost__isnull': True, f'{prefix}product__cost_price__isnull': True})


def _estimated(prefix=''):
    """Lines valued at the product's CURRENT cost because none was frozen."""
    return Q(**{f'{prefix}unit_cost__isnull': True, f'{prefix}product__cost_price__isnull': False})


def _sum(qs, expr):
    return qs.aggregate(total=Coalesce(Sum(expr), ZERO, output_field=_MONEY))['total']


# --- scope -------------------------------------------------------------------

def scoped_revenue_orders(user, start, end, branch_id=None):
    """The Completed orders a figure is built from: `user`'s branch scope, the
    Owner's optional branch pick, and the date range -- the same rows the Sales
    report totals (reports.services.scoped_sales_in_range)."""
    branch_id = owner_branch_choice(user, branch_id)
    rows = scoped_sales_in_range(user, start, end, branch_id=branch_id)
    return revenue_sales(rows).order_by()


def scoped_expenses(user, start, end, branch_id=None):
    """Expenses `user` may see in [start, end] (``date`` is a plain date, so it
    compares directly -- no timezone conversion involved)."""
    from expenses.services import visible_expenses

    qs = visible_expenses(user, branch_id=owner_branch_choice(user, branch_id))
    qs = qs.filter(date__lte=end)
    if start is not None:
        qs = qs.filter(date__gte=start)
    return qs.order_by()


# --- per-branch figures ------------------------------------------------------

@dataclass
class Figures:
    """One profit-and-loss column: a branch, or the whole scope."""
    branch: Branch = None
    label: str = ''
    gross_sales: Decimal = ZERO
    refunds: Decimal = ZERO
    cogs: Decimal = ZERO
    expenses: Decimal = ZERO

    @property
    def net_sales(self):
        return self.gross_sales - self.refunds

    @property
    def gross_profit(self):
        return self.net_sales - self.cogs

    @property
    def net_profit(self):
        return self.gross_profit - self.expenses

    @property
    def gross_margin(self):
        return percent(self.gross_profit, self.net_sales)

    @property
    def net_margin(self):
        return percent(self.net_profit, self.net_sales)

    @property
    def status(self):
        """'profit' / 'loss' / 'breakeven' -- what the page colours green/red."""
        if self.net_profit > 0:
            return 'profit'
        if self.net_profit < 0:
            return 'loss'
        return 'breakeven'


def _grouped(qs, key, expr):
    """{branch_id: Decimal} for one aggregate grouped by branch."""
    rows = qs.order_by().values(key).annotate(total=Coalesce(Sum(expr), ZERO, output_field=_MONEY))
    return {row[key]: row['total'] for row in rows}


def cogs_total(orders):
    """COGS for the Completed `orders`: line costs, less the cost of refunded
    units that went back on the shelf."""
    lines = SalesItem.objects.filter(transaction__in=orders)
    restocked = RefundItem.objects.filter(
        refund__status=Refund.Status.APPROVED, condition=RefundItem.Condition.SELLABLE,
        sales_item__transaction__in=orders,
    )
    return _sum(lines, _line_cogs()) - _sum(restocked, _line_cogs('sales_item__', 'quantity'))


def figures_by_branch(user, start, end):
    """{branch_id (None = no branch): Figures} for every branch `user` may see,
    over [start, end]. The single computation behind the page, the export and
    the Dashboard card -- totals are sums of these, never recomputed."""
    orders = scoped_revenue_orders(user, start, end)
    expenses = scoped_expenses(user, start, end)

    gross = _grouped(orders, 'branch_id', 'total_amount')
    refunds = _grouped(
        Refund.objects.filter(transaction__in=orders, status=Refund.Status.APPROVED),
        'transaction__branch_id', 'total_amount',
    )
    line_cogs = _grouped(
        SalesItem.objects.filter(transaction__in=orders), 'transaction__branch_id', _line_cogs(),
    )
    returned_cogs = _grouped(
        RefundItem.objects.filter(
            refund__status=Refund.Status.APPROVED, condition=RefundItem.Condition.SELLABLE,
            sales_item__transaction__in=orders,
        ),
        'sales_item__transaction__branch_id', _line_cogs('sales_item__', 'quantity'),
    )
    spent = _grouped(expenses, 'branch_id', 'amount')

    ids = set(gross) | set(refunds) | set(line_cogs) | set(spent)
    # Every branch the user may see gets a row, even a quiet one -- a branch
    # that sold nothing and spent nothing is still a line in the comparison.
    allowed = branch_ids_for_user(user)
    branch_qs = Branch.objects.all() if allowed is None else Branch.objects.filter(pk__in=allowed)
    branches = {b.pk: b for b in branch_qs}
    ids |= set(branches)

    result = {}
    for bid in ids:
        branch = branches.get(bid) or (Branch.objects.filter(pk=bid).first() if bid else None)
        result[bid] = Figures(
            branch=branch,
            label=branch.branch_name if branch else 'No branch assigned',
            gross_sales=gross.get(bid, ZERO),
            refunds=refunds.get(bid, ZERO),
            cogs=line_cogs.get(bid, ZERO) - returned_cogs.get(bid, ZERO),
            expenses=spent.get(bid, ZERO),
        )
    return result


def combine(figures, label):
    """One Figures that is the sum of `figures` (an iterable)."""
    total = Figures(label=label)
    for f in figures:
        total.gross_sales += f.gross_sales
        total.refunds += f.refunds
        total.cogs += f.cogs
        total.expenses += f.expenses
    return total


def sorted_rows(by_branch):
    """The branch rows in display order: by name, the branchless row last."""
    named = sorted((f for bid, f in by_branch.items() if bid is not None), key=lambda f: f.label.lower())
    return named + ([by_branch[None]] if None in by_branch else [])


def month_to_date_net_profit(user, today, branch_id=None):
    """(Figures, start, end) for the current calendar month so far -- the
    Dashboard's Net Profit card. One cheap call, no trend or product work."""
    start = today.replace(day=1)
    by_branch = figures_by_branch(user, start, today)
    pick = owner_branch_choice(user, branch_id)
    chosen = [f for bid, f in by_branch.items() if not pick or bid == pick]
    return combine(chosen, ''), start, today


# --- the full report ---------------------------------------------------------

@dataclass
class MonthRow:
    label: str
    first: date
    partial: bool
    net_sales: Decimal
    cogs: Decimal
    expenses: Decimal

    @property
    def net_profit(self):
        return self.net_sales - self.cogs - self.expenses


@dataclass
class ProductRow:
    product_id: int
    name: str
    sku: str
    units: int
    net_sales: Decimal
    cogs: Decimal
    uncosted_units: int = 0
    uncosted_revenue: Decimal = ZERO
    estimated_lines: int = 0

    @property
    def gross_profit(self):
        return self.net_sales - self.cogs

    @property
    def margin(self):
        return percent(self.gross_profit, self.net_sales)

    @property
    def fully_costed(self):
        return self.uncosted_units == 0

    @property
    def cost_note(self):
        if self.uncosted_units:
            return 'No cost price'
        if self.estimated_lines:
            return 'Estimated cost'
        return ''


@dataclass
class Profitability:
    start: date
    end: date
    branch_filter: int            # the Owner's pick (0 = none)
    totals: Figures
    branches: list                # [Figures] -- every branch in the user's scope
    show_comparison: bool         # an Owner sees the per-branch table
    expenses_by_category: list    # [(category, amount, share %)]
    procurement_spend: Decimal    # information only, never subtracted
    months: list                  # [MonthRow]
    months_trimmed: bool
    products: list = field(default_factory=list)       # every product sold, [ProductRow]
    top_products: list = field(default_factory=list)
    bottom_products: list = field(default_factory=list)
    missing_cost: list = field(default_factory=list)   # [ProductRow] with uncosted units
    removed_product_uncosted: int = 0                  # uncosted units of deleted products
    estimated_lines: int = 0
    unallocated_sales: Decimal = ZERO                  # net sales not on any product line
    order_count: int = 0

    @property
    def has_data(self):
        t = self.totals
        return bool(self.order_count or t.expenses)

    @property
    def all_branches(self):
        """The branch rows added together -- the comparison table's footer."""
        return combine(self.branches, 'All branches')

    @property
    def missing_product_count(self):
        return len(self.missing_cost) + (1 if self.removed_product_uncosted else 0)

    @property
    def is_incomplete(self):
        return bool(self.missing_cost or self.removed_product_uncosted)


def month_spans(start, end):
    """[(first_in_range, last_in_range, month_first, month_last)] for every
    calendar month touched by [start, end]."""
    spans = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        spans.append((max(first, start), min(last, end), first, last))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return spans


def _monthly(qs, expr, date_field, spans, aware):
    """One query: this aggregate for each month in `spans` (conditional SUMs, so
    no timezone function runs in the database -- see core.timebounds)."""
    aggregates = {}
    for i, (lo, hi, _f, _l) in enumerate(spans):
        if aware:
            a, b = local_range_bounds(lo, hi)
            condition = Q(**{f'{date_field}__gte': a, f'{date_field}__lt': b})
        else:
            condition = Q(**{f'{date_field}__gte': lo, f'{date_field}__lte': hi})
        aggregates[f'm{i}'] = Coalesce(Sum(expr, filter=condition), ZERO, output_field=_MONEY)
    row = qs.aggregate(**aggregates)
    return [row[f'm{i}'] for i in range(len(spans))]


def _trend(orders, expenses, start, end):
    spans = month_spans(start, end)
    trimmed = len(spans) > MAX_TREND_MONTHS
    spans = spans[-MAX_TREND_MONTHS:]
    if not spans:
        return [], False

    when = 'transaction__transaction_date'
    approved = Refund.objects.filter(transaction__in=orders, status=Refund.Status.APPROVED)
    sellable = RefundItem.objects.filter(
        refund__status=Refund.Status.APPROVED, condition=RefundItem.Condition.SELLABLE,
        sales_item__transaction__in=orders,
    )
    gross = _monthly(orders, 'total_amount', 'transaction_date', spans, aware=True)
    refunds = _monthly(approved, 'total_amount', when, spans, aware=True)
    lines = _monthly(SalesItem.objects.filter(transaction__in=orders), _line_cogs(), when, spans, aware=True)
    restocked = _monthly(
        sellable, _line_cogs('sales_item__', 'quantity'), 'sales_item__transaction__transaction_date',
        spans, aware=True,
    )
    spent = _monthly(expenses, 'amount', 'date', spans, aware=False)

    rows = []
    for i, (lo, hi, first, last) in enumerate(spans):
        rows.append(MonthRow(
            label=f'{first:%b %Y}', first=first, partial=(lo != first or hi != last),
            net_sales=gross[i] - refunds[i], cogs=lines[i] - restocked[i], expenses=spent[i],
        ))
    return rows, trimmed


def _product_rows(orders):
    """Per-product sales, cost and completeness over the Completed `orders`."""
    lines = SalesItem.objects.filter(transaction__in=orders)
    sold = {
        row['product_id']: row for row in lines.order_by().values(
            'product_id', 'product__product_name', 'product__sku_code',
        ).annotate(
            units=Sum('quantity_sold'),
            revenue=Coalesce(Sum('subtotal'), ZERO, output_field=_MONEY),
            cogs=Coalesce(Sum(_line_cogs()), ZERO, output_field=_MONEY),
            uncosted_units=Coalesce(Sum('quantity_sold', filter=_uncosted()), 0),
            uncosted_revenue=Coalesce(Sum('subtotal', filter=_uncosted()), ZERO, output_field=_MONEY),
            estimated_lines=Count('pk', filter=_estimated()),
        )
    }
    approved = dict(RefundItem.objects.filter(
        refund__status=Refund.Status.APPROVED, sales_item__transaction__in=orders,
    ).order_by().values_list('sales_item__product_id').annotate(total=Sum('amount')))
    restocked = dict(RefundItem.objects.filter(
        refund__status=Refund.Status.APPROVED, condition=RefundItem.Condition.SELLABLE,
        sales_item__transaction__in=orders,
    ).order_by().values_list('sales_item__product_id').annotate(
        total=Coalesce(Sum(_line_cogs('sales_item__', 'quantity')), ZERO, output_field=_MONEY),
    ))

    rows = []
    for pid, r in sold.items():
        rows.append(ProductRow(
            product_id=pid,
            name=r['product__product_name'] or '(removed product)',
            sku=r['product__sku_code'] or '',
            units=r['units'] or 0,
            net_sales=r['revenue'] - approved.get(pid, ZERO),
            cogs=r['cogs'] - restocked.get(pid, ZERO),
            uncosted_units=r['uncosted_units'] or 0,
            uncosted_revenue=r['uncosted_revenue'],
            estimated_lines=r['estimated_lines'] or 0,
        ))
    rows.sort(key=lambda p: (-p.gross_profit, p.name.lower()))
    return rows


def _rank(products):
    """(most, least) profitable among products whose cost is fully known.
    A product with a missing cost looks wildly profitable (its cost counted as
    zero), so it is left out of the ranking and listed under the completeness
    note instead. A product never appears in both lists."""
    ranked = [p for p in products if p.product_id is not None and p.fully_costed]
    most = ranked[:TOP_N]
    shown = {p.product_id for p in most}
    least = [p for p in reversed(ranked) if p.product_id not in shown][:TOP_N]
    return most, least


def _expense_breakdown(expenses):
    totals = dict(expenses.values_list('category').annotate(total=Sum('amount')))
    grand = sum(totals.values(), ZERO)
    return [
        (category, totals.get(category, ZERO), percent(totals.get(category, ZERO), grand) if grand else None)
        for category, _label in Expense.Category.choices
    ]


def build_profitability(user, start, end, branch_id=None):
    """Everything the Profitability page and its exports show, for `user`.

    `start=None` is 'All time'. An Owner's `branch_id` narrows the figures to
    one branch; the per-branch comparison always lists every branch the user
    may see. A Branch Manager's own branch always wins, and one with no branch
    assigned sees nothing (the scope helpers fail closed).
    """
    pick = owner_branch_choice(user, branch_id)
    by_branch = figures_by_branch(user, start, end)
    rows = sorted_rows(by_branch)
    chosen = [f for bid, f in by_branch.items() if not pick or bid == pick]
    if pick:
        scope_label = chosen[0].label if chosen else 'Unknown branch'
    elif branch_ids_for_user(user) is None:
        scope_label = 'All branches'
    else:
        scope_label = chosen[0].label if chosen else 'No branch assigned'
    totals = combine(chosen, scope_label)

    orders = scoped_revenue_orders(user, start, end, branch_id=pick)
    expenses = scoped_expenses(user, start, end, branch_id=pick)

    # Procurement spend: net of supplier returns, the same figure the
    # Procurement report and the Dashboard call Net Procurement.
    po_qs = scoped_procurement_in_range(user, start, end, branch_id=pick)
    procurement_spend = procurement_cost_total(po_qs) - procurement_return_totals(po_qs)['net_adjustment']

    trend_start = start
    if trend_start is None:
        first_sale = orders.order_by('transaction_date').values_list('transaction_date', flat=True).first()
        first_expense = expenses.order_by('date').values_list('date', flat=True).first()
        candidates = [d for d in (
            timezone.localtime(first_sale).date() if first_sale else None, first_expense,
        ) if d]
        trend_start = min(candidates) if candidates else end
    months, trimmed = _trend(orders, expenses, trend_start, end)

    products = _product_rows(orders)
    most, least = _rank(products)
    missing = sorted(
        (p for p in products if p.uncosted_units and p.product_id is not None),
        key=lambda p: (-p.uncosted_revenue, p.name.lower()),
    )
    removed_uncosted = sum(p.uncosted_units for p in products if p.product_id is None)

    return Profitability(
        start=start, end=end, branch_filter=pick or 0, totals=totals, branches=rows,
        show_comparison=branch_ids_for_user(user) is None,
        expenses_by_category=_expense_breakdown(expenses),
        procurement_spend=procurement_spend,
        months=months, months_trimmed=trimmed,
        products=products, top_products=most, bottom_products=least,
        missing_cost=missing, removed_product_uncosted=removed_uncosted,
        estimated_lines=sum(p.estimated_lines for p in products),
        unallocated_sales=totals.net_sales - sum((p.net_sales for p in products), ZERO),
        order_count=orders.count(),
    )


def data_notes(result):
    """Plain-language notes about how complete the figures are, for the page and
    the exports. Empty when everything is fully costed."""
    notes = []
    n = result.missing_product_count
    if n:
        notes.append(
            f'{n} product{"" if n == 1 else "s"} {"has" if n == 1 else "have"} no cost price; '
            f'profit may be overstated.'
        )
    if result.estimated_lines:
        k = result.estimated_lines
        notes.append(
            f'{k} sale line{"" if k == 1 else "s"} had no cost recorded when sold and '
            f'{"was" if k == 1 else "were"} valued at the product\'s current cost price.'
        )
    if abs(result.unallocated_sales) >= Decimal('0.01'):
        notes.append(
            f'Product lines add up to PHP {_cents(result.totals.net_sales - result.unallocated_sales):,.2f}, '
            f'PHP {abs(_cents(result.unallocated_sales)):,.2f} '
            f'{"less" if result.unallocated_sales > 0 else "more"} than net sales; '
            f'the difference is order-level rounding or adjustments.'
        )
    return notes
