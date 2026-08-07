import csv
import math

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from datetime import datetime, timedelta

from billing.models import BillingRecord

from sales.models import SalesTransaction
from inventory.models import Batch, Inventory
from inventory.alerts import EXPIRY_WINDOW_DAYS, alert_summary
from procurement.models import Procurement
from branches.models import Branch
from accounts.models import User
from core.scoping import branch_ids_for_user
from core.permissions import OWNER, BRANCH_MANAGER, role_required
from core.timebounds import local_day_bounds, local_month_bounds, local_range_bounds, resolve_date_range


def get_user_branch(user):
    """Returns the branch a Branch Manager or Staff belongs to, or None."""
    try:
        if user.role == User.Role.BRANCH_MANAGER:
            return user.manager_profile.branch
        elif user.role == User.Role.STAFF:
            return user.staff_profile.branch
    except Exception:
        return None
    return None


def _scoped_querysets(user):
    """Sales/procurement/inventory querysets scoped to user's branch (all, for Owner)."""
    sales_qs = SalesTransaction.objects.all()
    procurement_qs = Procurement.objects.all()
    inventory_qs = Inventory.objects.select_related('product', 'branch').all()

    user_branch = get_user_branch(user)
    if user_branch:
        sales_qs = sales_qs.filter(branch=user_branch)
        procurement_qs = procurement_qs.filter(branch=user_branch)
        inventory_qs = inventory_qs.filter(branch=user_branch)

    return sales_qs, procurement_qs, inventory_qs, user_branch


# --- Dashboard KPI graph geometry --------------------------------------
# `SalesTransaction.transaction_date` is a DateTimeField, so day-bucketing
# happens in Python (not TruncDate/`.dates()`) — MySQL's CONVERT_TZ returns
# NULL under USE_TZ when the server has no zoneinfo tables loaded, same
# constraint already documented on `month_keys` in reports_dashboard below.
# `Procurement.order_date` is a plain DateField with no tz component, so it
# doesn't need this and is bucketed directly (see _dashboard_daily_series).

def _parse_iso_date(value):
    """A YYYY-MM-DD request param as a date, or None if absent/malformed."""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _kpi_graph_series(daily_values, day_labels, width=760, height=220, pad_x=24, pad_y=24,
                      x_fractions=None):
    """
    Builds the click-to-expand graph panel's polyline, baseline-anchored
    area-fill path, and day-labeled point list for one KPI's daily series
    — with flat-series (all-equal values) handling that renders a horizontal
    midline rather than dividing by zero.

    `x_fractions` (one 0..1 position per value) places points along the real
    time axis instead of spacing them evenly by index. The Avg. Transaction
    Value line needs this: it omits days with no transactions entirely
    (DESIGN.md §2a), so even spacing would draw a 5-day gap and a 1-day gap
    identically. Series covering every day in the range pass None and get
    even spacing, which for them is the same thing.
    """
    if not daily_values:
        return {'polyline': '', 'area': '', 'points': []}

    lo, hi = min(daily_values), max(daily_values)
    span = hi - lo
    n = len(daily_values)
    plot_width = width - 2 * pad_x
    baseline = height - pad_y

    if x_fractions is None:
        step = plot_width / (n - 1) if n > 1 else 0
        xs = [pad_x + i * step for i in range(n)]
    else:
        xs = [pad_x + f * plot_width for f in x_fractions]

    def y_for(v):
        if span == 0:
            return height / 2
        return pad_y + (1 - (v - lo) / span) * (height - 2 * pad_y)

    coords = [
        (round(x, 1), round(y_for(v), 1))
        for x, v in zip(xs, daily_values)
    ]
    points = [
        {'x': x, 'y': y, 'date': day_labels[i], 'value': daily_values[i]}
        for i, (x, y) in enumerate(coords)
    ]
    polyline = ' '.join(f'{x},{y}' for x, y in coords)
    line_path = ' '.join(f'L{x},{y}' for x, y in coords)
    area = f'M{coords[0][0]},{baseline} {line_path} L{coords[-1][0]},{baseline} Z'

    return {'polyline': polyline, 'area': area, 'points': points}


# --- Dashboard date-range query layer (DESIGN.md Section 2, #2a/#2e) -----
# The KPI rail, its click-to-expand graph panel, and the Sales-by-Branch
# donut are all range-scoped views of one daily series — see
# _dashboard_metrics, the single entry point that builds all of it. The
# fixed 7/30-day hero-card helpers this replaced are gone: every figure on
# those surfaces now comes from the resolved range, so nothing is left
# computing a window of its own.

def _dashboard_daily_series(sales_qs, procurement_qs, start, end):
    """
    Per-day {date, sales, txn_count, procurement_cost} for every calendar
    day in [start, end] inclusive, zero-filled for days with no rows.

    transaction_date is a DateTimeField, so its local day is taken via
    timezone.localtime() per row rather than a __date= lookup — see
    core/timebounds for why that lookup silently matches nothing on this
    MySQL deployment. order_date is a plain DateField with no tz component
    and buckets directly, with no per-row conversion.
    """
    n_days = (end - start).days + 1
    buckets = {
        start + timedelta(n): {'sales': 0, 'txn_count': 0, 'procurement_cost': 0}
        for n in range(n_days)
    }

    range_start, range_end = local_range_bounds(start, end)
    sales_rows = sales_qs.filter(
        transaction_date__gte=range_start, transaction_date__lt=range_end
    ).values_list('transaction_date', 'total_amount')
    for transaction_date, amount in sales_rows:
        day = timezone.localtime(transaction_date).date()
        if day in buckets:
            buckets[day]['sales'] += amount or 0
            buckets[day]['txn_count'] += 1

    procurement_rows = procurement_qs.filter(
        order_date__gte=start, order_date__lte=end
    ).values_list('order_date', 'total_cost')
    for order_date, cost in procurement_rows:
        if order_date in buckets:
            buckets[order_date]['procurement_cost'] += cost or 0

    return [
        {'date': start + timedelta(n), **buckets[start + timedelta(n)]}
        for n in range(n_days)
    ]


def _dashboard_range_totals(daily_series):
    """
    Sums a daily series into range totals, plus the range-wide Avg.
    Transaction Value (total sales ÷ total transaction count — one ratio
    over the whole range, not an average of daily averages). DESIGN.md
    §2a requires the KPI card and the chart's displayed total to agree
    exactly, so both must read this same number.
    """
    sales = sum(d['sales'] for d in daily_series)
    txn_count = sum(d['txn_count'] for d in daily_series)
    procurement_cost = sum(d['procurement_cost'] for d in daily_series)
    atv = (sales / txn_count) if txn_count else 0
    return {'sales': sales, 'txn_count': txn_count, 'procurement_cost': procurement_cost, 'atv': atv}


def _dashboard_atv_points(daily_series):
    """
    Per-day Avg. Transaction Value for the click-to-expand chart's ATV
    line only. DESIGN.md §2a: days with zero transactions are skipped
    entirely — no interpolation, no zero-fill, no rolling average — so the
    line connects only real per-day averages.
    """
    return [
        {'date': d['date'], 'value': d['sales'] / d['txn_count']}
        for d in daily_series if d['txn_count']
    ]


def _dashboard_metrics(sales_qs, procurement_qs, start, end):
    """
    The single data source for the Dashboard's date-range-scoped surfaces
    (DESIGN.md §2a hero cards + graph, §2c donut): a daily series for the
    selected range, range totals for the KPI cards, per-day points for the
    Avg. Transaction Value chart line, the previous equal-length window's
    totals (for the hero cards' delta %), and sales grouped by branch.
    Everything those surfaces render is derived from this, not re-queried.
    """
    daily = _dashboard_daily_series(sales_qs, procurement_qs, start, end)
    totals = _dashboard_range_totals(daily)
    atv_points = _dashboard_atv_points(daily)

    period_days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)
    prev_totals = _dashboard_range_totals(
        _dashboard_daily_series(sales_qs, procurement_qs, prev_start, prev_end)
    )

    range_start, range_end = local_range_bounds(start, end)
    branch_breakdown = _sales_by_branch_chart(
        sales_qs.filter(transaction_date__gte=range_start, transaction_date__lt=range_end)
    )

    # Per-branch totals for the previous window, so each donut legend row can
    # show a real trend pill instead of a rule invented from its share alone.
    # Accumulated in Python the same way _sales_by_branch_chart does, keeping
    # the two sides of the comparison built identically.
    prev_range_start, prev_range_end = local_range_bounds(prev_start, prev_end)
    prev_branch_totals = {}
    prev_rows = sales_qs.filter(
        transaction_date__gte=prev_range_start, transaction_date__lt=prev_range_end
    ).values_list('branch__branch_name', 'total_amount')
    for name, amount in prev_rows:
        key = name or 'Unassigned'
        prev_branch_totals[key] = prev_branch_totals.get(key, 0) + (amount or 0)

    return {
        'daily': daily,
        'totals': totals,
        'atv_points': atv_points,
        'prev_totals': prev_totals,
        'period_days': period_days,
        'branch_breakdown': branch_breakdown,
        'prev_branch_totals': prev_branch_totals,
    }


def _delta_pill(current, previous, compare_label):
    """
    Period-over-period change as a status pill (DESIGN.md §2a) — the shared
    .pill-ok/.pill-danger/.pill-neutral trio, not a bespoke up/down/flat set.

    Direction is reported uniformly (up = ok) across all four KPIs, matching
    both the approved mockup and the cards this replaces. Note that reads as
    "rising" rather than "good" on Procurement Cost; giving that card an
    inverted polarity is a product decision, not a rendering one.
    """
    if not previous:
        return {'text': f'No {compare_label} to compare', 'pill': 'pill-neutral'}
    pct = round(((float(current) - float(previous)) / float(previous)) * 100, 1)
    rising = pct >= 0
    return {
        'text': f'{"▲" if rising else "▼"} {"+" if rising else ""}{pct}% vs {compare_label}',
        'pill': 'pill-ok' if rising else 'pill-danger',
    }


def _plural(n, suffix='s'):
    return suffix if n != 1 else ''


def _dashboard_kpi_payload(metrics, start, end):
    """
    The four KPI cards (DESIGN.md §2a) and the graph series each one swaps
    into the shared panel, built together from a single metrics bundle.

    Built as one structure on purpose: §2a requires a card's headline figure
    and the graph panel's total for that same metric to agree exactly, so
    both read the identical value here rather than being computed twice.

    Values are cast to float because this is handed to the template through
    |json_script — Decimal is not JSON-serializable, and DjangoJSONEncoder
    would stringify it, leaving the client formatting "68585.00" as text.
    """
    totals, prev, daily = metrics['totals'], metrics['prev_totals'], metrics['daily']
    period_days = metrics['period_days']
    compare_label = f'previous {period_days} day{_plural(period_days)}'
    txn_count = totals['txn_count']

    def x_fraction(day):
        return 0.0 if period_days <= 1 else (day - start).days / (period_days - 1)

    every_day_labels = [d['date'].isoformat() for d in daily]
    atv_points = metrics['atv_points']

    specs = [
        {
            'key': 'total_sales', 'label': 'Total Sales', 'unit': 'currency',
            'stroke': 'var(--brand)',
            'value_display': f'₱{totals["sales"]:,.2f}',
            'foot': f'{period_days} day{_plural(period_days)} · {txn_count} transaction{_plural(txn_count)}',
            'delta': _delta_pill(totals['sales'], prev['sales'], compare_label),
            'values': [float(d['sales']) for d in daily],
            'labels': every_day_labels,
            'x_fractions': None,
        },
        {
            'key': 'avg_transaction_value', 'label': 'Avg. Transaction Value',
            'unit': 'currency', 'stroke': 'var(--brand)',
            'value_display': f'₱{totals["atv"]:,.2f}',
            'foot': f'₱{totals["sales"]:,.2f} ÷ {txn_count} transaction{_plural(txn_count)}',
            'delta': _delta_pill(totals['atv'], prev['atv'], compare_label),
            # Days with no transactions are absent entirely, and the x
            # positions keep the remaining ones on the real time axis.
            'values': [float(p['value']) for p in atv_points],
            'labels': [p['date'].isoformat() for p in atv_points],
            'x_fractions': [x_fraction(p['date']) for p in atv_points],
        },
        {
            'key': 'procurement_cost', 'label': 'Procurement Cost', 'unit': 'currency',
            'stroke': 'var(--ink-400)',
            'value_display': f'₱{totals["procurement_cost"]:,.2f}',
            'foot': f'{period_days} day{_plural(period_days)} of supplier spend',
            'delta': _delta_pill(totals['procurement_cost'], prev['procurement_cost'], compare_label),
            'values': [float(d['procurement_cost']) for d in daily],
            'labels': every_day_labels,
            'x_fractions': None,
        },
        {
            'key': 'total_transactions', 'label': 'Total Transactions', 'unit': 'count',
            'stroke': 'var(--ink-400)',
            'value_display': f'{txn_count:,}',
            'foot': f'across {period_days} day{_plural(period_days)}',
            'delta': _delta_pill(txn_count, prev['txn_count'], compare_label),
            'values': [float(d['txn_count']) for d in daily],
            'labels': every_day_labels,
            'x_fractions': None,
        },
    ]

    cards, graphs = [], {}
    for spec in specs:
        graph = _kpi_graph_series(spec['values'], spec['labels'], x_fractions=spec['x_fractions'])
        cards.append({
            'key': spec['key'], 'label': spec['label'],
            'value_display': spec['value_display'], 'foot': spec['foot'],
            'delta': spec['delta'],
        })
        graphs[spec['key']] = {
            'label': spec['label'], 'unit': spec['unit'], 'stroke': spec['stroke'],
            'valueDisplay': spec['value_display'], 'foot': spec['foot'],
            'delta': spec['delta'], **graph,
        }
    return cards, graphs


def _branch_legend_rows(breakdown, prev_branch_totals):
    """
    Donut legend rows with a per-branch status pill (DESIGN.md §2c).

    Status compares a branch's sales in the selected range against the same
    branch over the previous equal-length window — a real trend. The mockup's
    placeholder rule (share below 25% = "Watch") is deliberately not carried
    over: it would flag a perfectly healthy branch simply for being one of
    four, and would flag every branch once a fifth opened.

    The folded "Other branches" aggregate gets no pill at all — which
    branches it contains can differ between the two windows, so a trend on it
    would not describe a stable set.
    """
    rows = []
    for seg in breakdown['segments']:
        is_other = bool(breakdown['overflow']) and seg['name'] == 'Other branches'
        previous = prev_branch_totals.get(seg['name'])
        if is_other:
            status = None
        elif not previous:
            status = {'pill': 'pill-neutral', 'label': 'No prior data'}
        elif float(seg['value']) < float(previous):
            status = {'pill': 'pill-warn', 'label': 'Down'}
        else:
            status = {'pill': 'pill-ok', 'label': 'On track'}
        rows.append({**seg, 'status': status})
    return rows


# --- "Needs attention" list (DESIGN.md Section 2, #2b) -------------------
# Five routing rows: icon + label + .pill count + chevron. Counts come from
# the same querysets/engines the destination pages use, so the badge and the
# page it links to can never disagree.

def _attention_pill(count, escalated=False):
    """
    Pill variant for a count badge: nothing outstanding is `ok`, an
    outstanding count that carries a hard failure (critical stock, already-
    expired batches) is `danger`, anything else is `warn`.
    """
    if not count:
        return 'pill-ok'
    return 'pill-danger' if escalated else 'pill-warn'


def _needs_attention_rows(summary, procurement_qs, sales_qs):
    """
    Build the five Dashboard "needs attention" checks in the DESIGN.md §2b
    order, already split into the two groups the panel renders differently:

      actionable  nonzero count — a full row (icon, label, count badge,
                  chevron) linking to the page that owns the work.
      all_clear   zero count — collapses into a compact "All clear" chip,
                  per §2b. Still linked, so the check is reachable, but it
                  never takes a full row and never carries a count.

    Split here rather than in the template so the template is two plain
    loops: §2b's two presentations are a data distinction, not markup logic.

    Definitions, kept deliberately aligned with the destination pages:
      Low Stock      inventory.alerts.low_stock_alerts — the same engine the
                     Alerts Center and Restock Suggestions table already use.
      Expiring Soon  inventory.alerts.expiry_alerts — batches with stock left
                     that expire within EXPIRY_WINDOW_DAYS (14), plus any
                     already past expiry. Escalates to danger once a batch has
                     actually expired.
      Pending POs    Draft + Ordered, i.e. raised but not yet Received or
                     Cancelled. Same set procurement_list totals as open_value.
      Pending Orders Pending + Approved + Preparing, i.e. every order that is
                     not yet Completed or Cancelled ("unfulfilled").
      Overdue Bill.  NOT COMPUTABLE YET — BillingRecord has no due_date or
                     payment-terms field, so there is no date to be overdue
                     against. Hardcoded 0 rather than silently substituting
                     the Pending count, which would mean "outstanding", not
                     "overdue". Swap in a real query when due dates land.
    """
    low_count = summary['low_count']
    expiring_count = summary['expiring_count']
    expired_count = sum(
        1 for r in summary['expiry']
        if r['status'] == Batch.ExpiryStatus.EXPIRED
    )

    pending_pos = procurement_qs.filter(
        status__in=[Procurement.Status.DRAFT, Procurement.Status.ORDERED]
    ).count()

    pending_orders = sales_qs.filter(
        order_status__in=[
            SalesTransaction.OrderStatus.PENDING,
            SalesTransaction.OrderStatus.APPROVED,
            SalesTransaction.OrderStatus.PREPARING,
        ]
    ).count()

    # See docstring — no due-date field exists to compute this against.
    overdue_billing = 0

    rows = [
        {
            'key': 'low_stock', 'icon': 'alert', 'label': 'Low Stock',
            'count': low_count,
            'pill': _attention_pill(low_count, escalated=summary['critical_count'] > 0),
            'detail': (
                f'{low_count} product{_plural(low_count)} at or below the reorder level '
                f'across your branches.'
            ),
            'url': reverse('alerts_center') + '#low',
        },
        {
            'key': 'expiring', 'icon': 'clock', 'label': 'Expiring Soon',
            'count': expiring_count,
            'pill': _attention_pill(expiring_count, escalated=expired_count > 0),
            'detail': (
                f'{expiring_count} batch{"es" if expiring_count != 1 else ""} expiring within '
                f'{EXPIRY_WINDOW_DAYS} days or already past expiry.'
            ),
            'url': reverse('alerts_center') + '#expiring',
        },
        {
            'key': 'pending_pos', 'icon': 'truck', 'label': 'Pending POs',
            'count': pending_pos,
            'pill': _attention_pill(pending_pos),
            'detail': (
                f'{pending_pos} purchase order{_plural(pending_pos)} raised but not yet '
                f'received from the supplier.'
            ),
            'url': reverse('procurement_list'),
        },
        {
            'key': 'pending_orders', 'icon': 'cart', 'label': 'Unfulfilled Orders',
            'count': pending_orders,
            'pill': _attention_pill(pending_orders),
            'detail': (
                f'{pending_orders} order{_plural(pending_orders)} still pending, approved or '
                f'preparing — none completed yet.'
            ),
            'url': reverse('transaction_list'),
        },
        {
            'key': 'overdue_billing', 'icon': 'peso', 'label': 'Overdue Billing',
            'count': overdue_billing,
            'pill': _attention_pill(overdue_billing),
            'detail': (
                f'{overdue_billing} billing record{_plural(overdue_billing)} past its due date.'
            ),
            'url': reverse('billing_list') + f'?status={BillingRecord.PaymentStatus.PENDING}',
        },
    ]

    actionable = [r for r in rows if r['count']]
    checks = len(rows)
    if actionable:
        needs = 'needs' if len(actionable) == 1 else 'need'
        subtitle = f'{len(actionable)} of {checks} checks {needs} action right now'
    else:
        subtitle = f'All {checks} checks clear — nothing needs action right now'

    return {
        'actionable': actionable,
        'all_clear': [r for r in rows if not r['count']],
        'subtitle': subtitle,
    }


@login_required(login_url='login')
def operational_dashboard(request):
    """
    The Dashboard — a live operational snapshot: hero KPI cards (with a
    short trend sparkline each), per-branch status, restock alerts, recent
    orders. Deeper historical/analytical breakdowns (branch & product
    performance, filterable by month) still live on the Reports page
    instead (see reports_dashboard) — the hero sparklines are a short,
    fixed recent-trend glance, not a substitute for that analysis.
    Branch-scoped: managers/staff see only their own branch.
    """
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
    ]:
        messages.error(request, 'You do not have permission to view this page.')
        return render(request, 'reports/no_access.html')

    sales_qs, procurement_qs, _inventory_qs, user_branch = _scoped_querysets(request.user)

    now = timezone.localtime()
    today = now.date()

    # today_sales feeds _branch_status_rows below (each branch's share of
    # today's total), even though it's no longer its own hero card.
    # Range comparison, not __date= — see local_day_bounds for why that
    # lookup silently returns nothing on this MySQL deployment.
    _today_start, _today_end = local_day_bounds(today)
    today_sales = sales_qs.filter(
        transaction_date__gte=_today_start, transaction_date__lt=_today_end
    ).aggregate(total=Sum('total_amount'))['total'] or 0

    # --- Date-range-scoped KPI rail, graph panel and donut (DESIGN.md §2a/
    # §2c). All three read one _dashboard_metrics bundle for the resolved
    # range, so a card, the graph it opens, and the donut can never disagree.
    # The range control itself is task 9 — until it ships these params have
    # no UI, and an absent or malformed one falls back to the last 30 days.
    range_start, range_end, active_range = resolve_date_range(
        request.GET.get('range'),
        _parse_iso_date(request.GET.get('date_from')),
        _parse_iso_date(request.GET.get('date_to')),
        today=today,
    )
    metrics = _dashboard_metrics(sales_qs, procurement_qs, range_start, range_end)
    kpi_cards, kpi_graphs = _dashboard_kpi_payload(metrics, range_start, range_end)
    branch_breakdown = metrics['branch_breakdown']
    branch_legend = _branch_legend_rows(branch_breakdown, metrics['prev_branch_totals'])

    # --- Restock suggestions + "needs attention" list (same engine as the
    # Alerts Center — one alert_summary() call feeds both) ---
    branch_ids = branch_ids_for_user(request.user)
    summary = alert_summary(branch_ids)
    restock = summary['low_stock']
    needs_attention = _needs_attention_rows(summary, procurement_qs, sales_qs)

    # --- Branch status row: 3-second scan of each branch's sales today ---
    branches = Branch.objects.all() if branch_ids is None else Branch.objects.filter(id__in=branch_ids)
    branch_status = _branch_status_rows(branches, sales_qs, restock, today, today_sales, now=now)
    # Only surface the tinted status cards when a branch actually tripped a
    # threshold; otherwise the template falls back to a plain neutral summary
    # rather than painting a full red row over an uneventful day.
    branch_alerts_present = any(row['status'] != 'ok' for row in branch_status)

    recent_orders = sales_qs.select_related('branch', 'customer').order_by(
        '-transaction_date'
    )[:6]

    return render(request, 'reports/dashboard.html', {
        # KPI rail + the graph panel they tab into + the Sales-by-Branch
        # donut, all derived from the one resolved range (DESIGN.md §2a/§2c).
        'kpi_cards': kpi_cards,
        'kpi_graphs': kpi_graphs,
        # Default panel state (Total Sales) — server-rendered so the panel
        # holds real content before dashboard-kpi.js takes over on click.
        'default_kpi_key': 'total_sales',
        'default_kpi_graph': kpi_graphs['total_sales'],
        'branch_chart': branch_breakdown,
        'branch_legend': branch_legend,
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        # low_stock_count still feeds the Restock Suggestions header; the four
        # quiet stat tiles it used to sit beside are gone (duplicated the Needs
        # Attention rows / were static catalog facts, not daily decisions).
        'low_stock_count': len(restock),
        'restock': restock[:6],
        'needs_attention': needs_attention,
        'branch_status': branch_status,
        'branch_alerts_present': branch_alerts_present,
        'recent_orders': recent_orders,
        'is_owner': request.user.role == User.Role.OWNER,
        'user_branch': user_branch,
        'today': today,
        'active_page': 'dashboard',
    })


@role_required(OWNER, BRANCH_MANAGER)
def reports_dashboard(request):
    """
    The Reports hub — opens straight onto the Sales Report: every transaction
    the user's role can see, filterable by branch (Owner only — a Manager's
    own branch is already the whole story) and by month, with the
    same CSV/print-to-PDF export as the other per-process reports.
    """
    user_branch = get_user_branch(request.user)
    branch_ids = branch_ids_for_user(request.user)
    show_branch_filter = branch_ids is None  # Owner only

    sales_qs = SalesTransaction.objects.select_related('branch', 'staff', 'customer').all()
    if branch_ids is not None:
        sales_qs = sales_qs.filter(branch_id__in=branch_ids)

    # Month options are drawn from whatever's actually in the (branch-scoped)
    # data, so the dropdown never offers a month with nothing to show.
    # Computed in Python (not queryset.dates()) because MySQL's CONVERT_TZ
    # under USE_TZ returns NULL when the server has no zoneinfo tables loaded.
    month_keys = sorted({
        timezone.localtime(dt).strftime('%Y-%m')
        for dt in sales_qs.values_list('transaction_date', flat=True)
    }, reverse=True)
    month_options = [
        {'value': key, 'label': datetime.strptime(key, '%Y-%m').strftime('%B %Y')}
        for key in month_keys
    ]

    branch_filter = request.GET.get('branch', '')
    month_filter = request.GET.get('month', '')

    if branch_filter and show_branch_filter:
        sales_qs = sales_qs.filter(branch_id=branch_filter)
    else:
        branch_filter = ''

    if month_filter:
        try:
            year, month = (int(part) for part in month_filter.split('-'))
            # Range comparison, NOT __year=/__month= — those go through
            # CONVERT_TZ on MySQL and silently match nothing here. See
            # local_month_bounds.
            month_start, month_end = local_month_bounds(year, month)
            sales_qs = sales_qs.filter(
                transaction_date__gte=month_start, transaction_date__lt=month_end
            )
        except ValueError:
            month_filter = ''

    sales_qs = sales_qs.order_by('-transaction_date')

    total_orders = sales_qs.count()
    total_sales = sales_qs.aggregate(t=Sum('total_amount'))['t'] or 0
    avg_order_value = (total_sales / total_orders) if total_orders else 0
    online_orders = sales_qs.filter(
        transaction_type=SalesTransaction.TransactionType.ONLINE
    ).count()

    # Donut panel — built from the same filtered sales_qs as the KPIs/table
    # above, so branch + month selections carry into it (DESIGN.md 2c).
    branch_chart = _sales_by_branch_chart(sales_qs)

    return render(request, 'reports/reports_home.html', {
        'transactions': sales_qs,
        'branch_chart': branch_chart,
        'total_orders': total_orders,
        'total_sales': total_sales,
        'avg_order_value': avg_order_value,
        'online_orders': online_orders,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        'month_filter': month_filter,
        'month_options': month_options,
        'is_owner': request.user.role == User.Role.OWNER,
        'user_branch': user_branch,
        'active_page': 'reports',
    })


# --- Sales by Branch donut (Reports → Sales tab, DESIGN.md Section 2, #2c) ---
# One-hue green scale, biggest share first. Identity is carried by the legend
# (name + ₱ + % on every row), never by colour alone — which is what keeps the
# scale legible despite the caveat below.
#
# Contrast vs the white .card surface, recomputed from the dataviz validator's
# thresholds (ported to Python — no node on the build box):
#   --brand      #2E9E6B  3.38:1  PASS
#   --green-300  #9CCDB4  1.78:1  under the 2.0:1 ordinal light-end floor
#   --action     #1E7A4F  5.31:1  PASS
#
# Slot 3 was --green-100 (#E3F3EB, 1.15:1 — effectively invisible on white);
# swapping it for --action lifts the worst slot to 1.78:1 and the third
# segment specifically from 1.15:1 to 5.31:1.
#
# Two known, accepted trade-offs of that swap:
#   1. --green-300 is now the palest step at 1.78:1, still just under the
#      2.0:1 floor. It is the binding constraint, not slot 3 — no choice of
#      third colour can clear the floor while slot 2 stays --green-300.
#   2. The scale is no longer lightness-monotone (L 0.624 / 0.807 / 0.516,
#      dark→light→dark), so it reads as three distinct steps rather than a
#      ranked ramp. Harmless here because rank is already stated numerically
#      in the legend.
# A fully-passing monotone alternative, if the ramp is ever revisited:
# --action-700 / --action / --brand (7.59 / 5.31 / 3.38:1, all checks PASS).
SALES_BRANCH_RAMP = ['var(--brand)', 'var(--green-300)', 'var(--action)']
SALES_BRANCH_OTHER_COLOUR = 'var(--ink-400)'

# Donut geometry — a stroked circle with a dash pattern, so each segment is one
# <circle> and no arc-path maths is needed (a 100%-single-segment donut, which
# is what a one-branch store renders, stays a clean full ring).
DONUT_RADIUS = 60
DONUT_STROKE = 26
DONUT_BOX = 160
DONUT_GAP = 2.0  # surface-coloured gap between adjacent fills


def _sales_by_branch_chart(sales_qs):
    """
    "Sales by Branch" donut for the Reports Sales tab.

    Aggregates whatever `sales_qs` the Sales tab has already filtered (branch
    dropdown + month dropdown), so the donut always describes the same rows as
    the KPI cards and the transactions table beneath it.

    NOTE: this is a new per-branch rollup, not the Dashboard's. The Dashboard's
    _branch_status_rows() is hard-scoped to *today* (today vs yesterday delta,
    share of today's sales, live stock-alert counts) and takes restock/today/
    total_today_sales as arguments — it cannot answer "sales per branch over
    the selected month", which is what this panel needs.

    Branches past the ramp's length fold into one "Other branches" segment
    rather than getting improvised colours.
    """
    by_branch = {}
    for name, amount in sales_qs.values_list('branch__branch_name', 'total_amount'):
        key = name or 'Unassigned'
        by_branch[key] = by_branch.get(key, 0) + (amount or 0)

    rows = sorted(by_branch.items(), key=lambda kv: -kv[1])
    total = sum(by_branch.values())
    if not total:
        return {'total': 0, 'segments': [], 'circumference': 0, 'radius': DONUT_RADIUS,
                'stroke': DONUT_STROKE, 'box': DONUT_BOX, 'centre': DONUT_BOX / 2,
                'branch_count': 0, 'overflow': 0}

    # With more branches than ramp steps, the last slot becomes a single
    # "Other branches" aggregate. It has to swallow everything from
    # limit-1 onwards (not just limit onwards), or the branch sitting in the
    # last named slot is dropped and the segments stop summing to 100%.
    limit = len(SALES_BRANCH_RAMP)
    if len(rows) > limit:
        folded = rows[limit - 1:]
        head = rows[:limit - 1] + [('Other branches', sum(v for _, v in folded))]
    else:
        folded = []
        head = rows

    circumference = 2 * math.pi * DONUT_RADIUS
    gap = DONUT_GAP if len(head) > 1 else 0

    segments = []
    cursor = 0.0
    for i, (name, value) in enumerate(head):
        fraction = float(value) / float(total)
        arc = fraction * circumference
        is_other = bool(folded) and i == len(head) - 1
        segments.append({
            'name': name,
            'value': value,
            'pct': round(fraction * 100, 1),
            'colour': SALES_BRANCH_OTHER_COLOUR if is_other else SALES_BRANCH_RAMP[i],
            # "<dash> <rest>" + a negative offset walks each segment round the ring.
            'dasharray': f'{max(arc - gap, 0.5):.2f} {circumference - max(arc - gap, 0.5):.2f}',
            'dashoffset': f'{-cursor:.2f}',
        })
        cursor += arc

    return {
        'total': total,
        'segments': segments,
        'circumference': round(circumference, 2),
        'radius': DONUT_RADIUS,
        'stroke': DONUT_STROKE,
        'box': DONUT_BOX,
        'centre': DONUT_BOX / 2,
        # Real branch count for the donut's centre label — the segment count
        # understates it once branches are folded into "Other branches".
        'branch_count': len(rows),
        'overflow': len(folded),
    }


def _inventory_unit_cost(inv):
    """Cost basis for one unit — wholesale price where known, else retail."""
    if not inv.product:
        return 0
    return inv.product.wholesale_price or inv.product.unit_price or 0


def _inventory_branch_summary(records):
    """Per-branch rollup for the Inventory Report's summary table + chart.
    Expects each `inv` to already carry a `.value` attribute (see inventory_report)."""
    by_branch = {}
    for inv in records:
        if not inv.branch:
            continue
        row = by_branch.setdefault(inv.branch.branch_name, {
            'product_ids': set(), 'units': 0, 'value': 0, 'low_critical': 0,
        })
        row['product_ids'].add(inv.product_id)
        row['units'] += inv.quantity_on_hand
        row['value'] += inv.value
        if inv.restock:
            row['low_critical'] += 1

    summary = [
        {
            'name': name,
            'product_lines': len(row['product_ids']),
            'units': row['units'],
            'value': row['value'],
            'low_critical': row['low_critical'],
        }
        for name, row in by_branch.items()
    ]
    summary.sort(key=lambda r: -r['value'])
    return summary


def _compact_currency(value):
    """Auto-compact a peso amount: 850 / 30k / 1.2M — for axis ticks."""
    value = float(value)
    if value >= 1_000_000:
        return f'{value / 1_000_000:.1f}M'
    if value >= 1_000:
        return f'{value / 1_000:.0f}k'
    return f'{value:.0f}'


def _nice_step(peak, tick_count=4):
    """Round `peak / tick_count` up to a clean 1/2/5×10^n step (axis ticks)."""
    raw_step = float(peak) / tick_count
    magnitude = 10 ** math.floor(math.log10(raw_step))
    residual = raw_step / magnitude
    if residual > 5:
        step = 10 * magnitude
    elif residual > 2:
        step = 5 * magnitude
    elif residual > 1:
        step = 2 * magnitude
    else:
        step = magnitude
    return step, math.ceil(peak / step)


def _inventory_value_chart(branch_summary):
    """
    Horizontal bar chart geometry for "Inventory Value by Branch" — one
    series (value), so no legend; bar length is a % of a nice rounded max.
    """
    peak = max((row['value'] for row in branch_summary), default=0)
    if not peak:
        return {'bars': [], 'ticks': []}

    step, tick_count = _nice_step(peak)
    chart_max = step * tick_count
    ticks = [_compact_currency(step * i) for i in range(tick_count + 1)]

    bars = [
        {
            'name': row['name'],
            'value': row['value'],
            'pct': round((float(row['value']) / float(chart_max)) * 100, 1),
        }
        for row in branch_summary
    ]
    return {'bars': bars, 'ticks': ticks}


# Status is a reserved, fixed scale (never re-derived per chart) — distinct
# from the app's pill colours because pill-info and pill-ok share one green
# dot (#2E9E6B) in this design system, which would make "Ordered" and
# "Received" indistinguishable in a pie chart's fill-only encoding. Validated
# with the dataviz skill's validator (all-pairs, light mode): PASS.
PROCUREMENT_STATUS_COLOURS = {
    Procurement.Status.DRAFT: '#8A968F',
    Procurement.Status.ORDERED: '#3B6FD4',
    Procurement.Status.RECEIVED: '#2E9E6B',
    Procurement.Status.CANCELLED: '#B42318',
}


def _procurement_status_chart(status_counts):
    """
    PO Status Breakdown pie — one status scale, so a legend (dot + label +
    count) carries identity; never colour alone. `status_counts` keys must
    already be in Procurement.Status order (Draft, Ordered, Received,
    Cancelled) so the legend and the gradient segments line up.
    """
    total = sum(status_counts.values())
    if not total:
        return {'total': 0, 'segments': [], 'gradient': ''}

    segments = []
    stops = []
    cursor = 0.0
    for status, count in status_counts.items():
        if not count:
            continue
        pct = count / total * 100
        colour = PROCUREMENT_STATUS_COLOURS.get(status, '#8A968F')
        stops.append(f'{colour} {cursor:.2f}% {cursor + pct:.2f}%')
        segments.append({'label': status, 'count': count, 'colour': colour})
        cursor += pct

    return {'total': total, 'segments': segments, 'gradient': ', '.join(stops)}


def _billing_payment_breakdown(completed_records, total_revenue):
    """Revenue-by-payment-method rows for the Billing Report (completed orders only)."""
    by_method = {}
    for r in completed_records:
        row = by_method.setdefault(r.payment_method, {'count': 0, 'total': 0})
        row['count'] += 1
        row['total'] += r.amount_due

    rows = [
        {
            'method': method,
            'count': row['count'],
            'total': row['total'],
            'pct': round((float(row['total']) / float(total_revenue)) * 100, 1) if total_revenue else 0,
        }
        for method, row in by_method.items()
    ]
    rows.sort(key=lambda r: -r['total'])
    return rows


# --- Per-branch status thresholds ---------------------------------------
# A branch only goes amber/red on evidence. "No sales recorded yet" is NOT
# evidence — it is the normal state of every branch at 9am, and the normal
# state of every branch in a store that has not gone live yet.
BRANCH_BASELINE_DAYS = 7      # trailing window for the sales baseline (excludes today)
BRANCH_SLOW_RATIO = 0.5       # today's pace below half the baseline pace = slow
BRANCH_MIN_DAY_ELAPSED = 0.25  # ignore the sales signal before ~6am local


def _branch_status_rows(branches, sales_qs, restock, today, total_today_sales, now=None):
    """
    Per-branch status for the Dashboard's "at a glance" row.

    status is driven by two real signals, never by absence of data:
      'critical'  the branch has stock at/below half its reorder level
                  (risk == 'Critical' in inventory.alerts).
      'watch'     the branch has any low-stock row, OR today's sales are
                  pacing under BRANCH_SLOW_RATIO of its own trailing
                  BRANCH_BASELINE_DAYS-day average.
      'ok'        everything else, including "quiet so far today".

    The sales comparison is PRO-RATED by how much of the local day has
    elapsed, so a branch is not called slow at 8am for having done less than
    a full day's takings, and is skipped entirely before
    BRANCH_MIN_DAY_ELAPSED (the ratio is meaningless at 00:05) or when the
    branch has no trailing history to compare against.
    """
    now = now or timezone.localtime()
    today_start, today_end = local_day_bounds(today)
    baseline_start, _ = local_day_bounds(today - timedelta(days=BRANCH_BASELINE_DAYS))

    day_elapsed = (now - today_start).total_seconds() / 86400.0
    day_elapsed = min(max(day_elapsed, 0.0), 1.0)

    rows = []
    for branch in branches:
        b_today_sales = sales_qs.filter(
            branch=branch, transaction_date__gte=today_start, transaction_date__lt=today_end
        ).aggregate(total=Sum('total_amount'))['total'] or 0

        baseline_total = sales_qs.filter(
            branch=branch, transaction_date__gte=baseline_start, transaction_date__lt=today_start
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        baseline_daily = float(baseline_total) / BRANCH_BASELINE_DAYS

        branch_alerts = [r for r in restock if r['branch'] and r['branch'].id == branch.id]
        critical_count = sum(1 for r in branch_alerts if r['risk'] == 'Critical')

        # Only a branch with its own trading history can be called slow.
        sales_slow = (
            baseline_daily > 0
            and day_elapsed >= BRANCH_MIN_DAY_ELAPSED
            and float(b_today_sales) < BRANCH_SLOW_RATIO * baseline_daily * day_elapsed
        )

        if critical_count:
            status = 'critical'
        elif branch_alerts or sales_slow:
            status = 'watch'
        else:
            status = 'ok'

        rows.append({
            'branch': branch,
            'today_sales': b_today_sales,
            'baseline_daily': baseline_daily,
            'sales_slow': sales_slow,
            'share_pct': round((float(b_today_sales) / float(total_today_sales)) * 100, 1)
                if total_today_sales else 0,
            'low_stock_count': len(branch_alerts),
            'critical_count': critical_count,
            'status': status,
        })
    return rows


# =============================================================================
# CSV EXPORT
# One entry point for every report so the columns stay consistent.
# PDF: the report pages carry a print stylesheet — "Export PDF" opens the
# browser's print dialog (Save as PDF). That keeps the system dependency-free
# and working offline, per the deployment constraints.
# =============================================================================

@login_required(login_url='login')
def export_csv(request, kind):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to export reports.')
        return render(request, 'reports/no_access.html')

    user_branch = get_user_branch(request.user)
    today = timezone.localtime().date()

    response = HttpResponse(content_type='text/csv')
    scope = user_branch.branch_name.replace(' ', '-') if user_branch else 'all-branches'
    response['Content-Disposition'] = (
        f'attachment; filename="gerlyn-{kind}-{scope}-{today}.csv"'
    )
    writer = csv.writer(response)

    if kind == 'sales':
        qs = SalesTransaction.objects.select_related('branch', 'staff', 'customer')
        if user_branch:
            qs = qs.filter(branch=user_branch)
        writer.writerow([
            'Order Number', 'Date', 'Branch', 'Customer', 'Recorded By',
            'Channel', 'Payment Method', 'Order Status', 'Total (PHP)',
        ])
        for t in qs.order_by('-transaction_date'):
            writer.writerow([
                t.order_number,
                timezone.localtime(t.transaction_date).strftime('%Y-%m-%d %H:%M'),
                t.branch.branch_name if t.branch else '',
                (t.customer.get_full_name() or t.customer.username) if t.customer else 'Walk-in',
                t.staff.username if t.staff else '',
                t.transaction_type, t.mode_of_payment, t.order_status, t.total_amount,
            ])

    elif kind == 'inventory':
        qs = Inventory.objects.select_related('product', 'branch')
        if user_branch:
            qs = qs.filter(branch=user_branch)
        writer.writerow([
            'SKU', 'Product', 'Branch', 'Quantity On Hand', 'Reorder Level',
            'Needs Restock', 'Nearest Expiry', 'Last Updated',
        ])
        for i in qs.order_by('product__product_name'):
            writer.writerow([
                i.product.sku_code if i.product else '',
                i.product.product_name if i.product else '',
                i.branch.branch_name if i.branch else '',
                i.quantity_on_hand, i.reorder_level,
                'Yes' if i.restock else 'No',
                i.expiry_date or '',
                timezone.localtime(i.last_stock_update).strftime('%Y-%m-%d'),
            ])

    elif kind == 'procurement':
        qs = Procurement.objects.select_related('branch', 'supplier')
        if user_branch:
            qs = qs.filter(branch=user_branch)
        writer.writerow([
            'PO Number', 'Branch', 'Supplier', 'Order Date', 'Delivery Date',
            'Status', 'Receipt Flag', 'Total Cost (PHP)',
        ])
        for p in qs.order_by('-order_date'):
            supplier = ''
            if p.supplier:
                supplier = getattr(
                    getattr(p.supplier, 'supplier_profile', None),
                    'supplier_name', p.supplier.username,
                )
            writer.writerow([
                p.po_number, p.branch.branch_name if p.branch else '', supplier,
                p.order_date, p.delivery_date or '', p.status, p.receipt_flag, p.total_cost,
            ])

    elif kind == 'billing':
        qs = BillingRecord.objects.select_related('transaction', 'transaction__branch')
        if user_branch:
            qs = qs.filter(transaction__branch=user_branch)
        writer.writerow([
            'Order Number', 'Branch', 'Date Recorded', 'Amount Due (PHP)',
            'Payment Method', 'Reference Number', 'Status', 'Confirmed By',
        ])
        for r in qs.order_by('-date_recorded'):
            writer.writerow([
                r.transaction.order_number if r.transaction else '',
                r.transaction.branch.branch_name if r.transaction and r.transaction.branch else '',
                timezone.localtime(r.date_recorded).strftime('%Y-%m-%d %H:%M'),
                r.amount_due, r.payment_method, r.reference_number, r.status,
                r.confirmed_by.username if r.confirmed_by else '',
            ])
    else:
        messages.error(request, f'Unknown report "{kind}".')
        return redirect('reports_dashboard')

    return response


@login_required(login_url='login')
def billing_report(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to view this report.')
        return render(request, 'reports/no_access.html')

    user_branch = get_user_branch(request.user)
    branch_ids = branch_ids_for_user(request.user)
    show_branch_filter = branch_ids is None  # Owner only

    records_qs = BillingRecord.objects.select_related('transaction', 'transaction__branch')
    if branch_ids is not None:
        records_qs = records_qs.filter(transaction__branch_id__in=branch_ids)

    branch_filter = request.GET.get('branch', '')
    if branch_filter and show_branch_filter:
        records_qs = records_qs.filter(transaction__branch_id=branch_filter)
    else:
        branch_filter = ''

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    # Half-open aware-datetime bounds, NOT date_recorded__date__gte/__lte —
    # `date_recorded` is a DateTimeField, so those lookups go through
    # CONVERT_TZ on MySQL and silently match zero rows here, which made both
    # date inputs return an empty report for every range. See core.timebounds.
    # date_to is inclusive of its whole day: take the END of that day and
    # compare with __lt.
    if date_from:
        try:
            from_start, _ = local_day_bounds(
                datetime.strptime(date_from, '%Y-%m-%d').date()
            )
            records_qs = records_qs.filter(date_recorded__gte=from_start)
        except ValueError:
            date_from = ''
    if date_to:
        try:
            _, to_end = local_day_bounds(
                datetime.strptime(date_to, '%Y-%m-%d').date()
            )
            records_qs = records_qs.filter(date_recorded__lt=to_end)
        except ValueError:
            date_to = ''

    records = list(records_qs.order_by('-date_recorded'))

    pending_records = [r for r in records if r.status == BillingRecord.PaymentStatus.PENDING]
    outstanding = sum(r.amount_due for r in pending_records)
    collected_records = [r for r in records if r.status == BillingRecord.PaymentStatus.PAID]
    collected = sum(r.amount_due for r in collected_records)

    completed_records = [
        r for r in records
        if r.transaction and r.transaction.order_status == SalesTransaction.OrderStatus.COMPLETED
    ]
    total_revenue = sum(r.amount_due for r in completed_records)

    payment_breakdown = _billing_payment_breakdown(completed_records, total_revenue)

    return render(request, 'reports/billing_report.html', {
        'records': records,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        'date_from': date_from,
        'date_to': date_to,
        'completed_count': len(completed_records),
        'total_revenue': total_revenue,
        'collected': collected,
        'collected_count': len(collected_records),
        'outstanding': outstanding,
        'pending_count': len(pending_records),
        'payment_breakdown': payment_breakdown,
        'user_branch': user_branch,
        'active_page': 'reports',
    })


@login_required(login_url='login')
def sales_report(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to view this report.')
        return render(request, 'reports/no_access.html')

    sales_qs = SalesTransaction.objects.select_related('branch', 'staff', 'customer').all()
    user_branch = get_user_branch(request.user)
    if user_branch:
        sales_qs = sales_qs.filter(branch=user_branch)

    sales_qs = sales_qs.order_by('-transaction_date')
    return render(request, 'reports/sales_report.html', {'transactions': sales_qs})


@login_required(login_url='login')
def inventory_report(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to view this report.')
        return render(request, 'reports/no_access.html')

    user_branch = get_user_branch(request.user)
    branch_ids = branch_ids_for_user(request.user)
    show_branch_filter = branch_ids is None  # Owner only

    inventory_qs = Inventory.objects.select_related('product', 'branch').all()
    if branch_ids is not None:
        inventory_qs = inventory_qs.filter(branch_id__in=branch_ids)

    branch_filter = request.GET.get('branch', '')
    if branch_filter and show_branch_filter:
        inventory_qs = inventory_qs.filter(branch_id=branch_filter)
    else:
        branch_filter = ''

    records = list(inventory_qs.order_by('branch__branch_name', 'product__product_name'))
    for inv in records:
        inv.value = _inventory_unit_cost(inv) * inv.quantity_on_hand

    total_units = sum(inv.quantity_on_hand for inv in records)
    total_value = sum(inv.value for inv in records)
    low_critical_count = sum(1 for inv in records if inv.restock)
    product_lines = len({inv.product_id for inv in records if inv.product_id})

    branch_summary = _inventory_branch_summary(records)
    chart = _inventory_value_chart(branch_summary)

    return render(request, 'reports/inventory_report.html', {
        'inventory_records': records,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        'total_value': total_value,
        'total_units': total_units,
        'low_critical_count': low_critical_count,
        'product_lines': product_lines,
        'branch_summary': branch_summary,
        'chart': chart,
        'user_branch': user_branch,
        'active_page': 'reports',
    })


@login_required(login_url='login')
def procurement_report(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to view this report.')
        return render(request, 'reports/no_access.html')

    user_branch = get_user_branch(request.user)
    branch_ids = branch_ids_for_user(request.user)
    show_branch_filter = branch_ids is None  # Owner only

    procurement_qs = Procurement.objects.select_related('branch', 'supplier').all()
    if branch_ids is not None:
        procurement_qs = procurement_qs.filter(branch_id__in=branch_ids)

    branch_filter = request.GET.get('branch', '')
    if branch_filter and show_branch_filter:
        procurement_qs = procurement_qs.filter(branch_id=branch_filter)
    else:
        branch_filter = ''

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    if date_from:
        try:
            procurement_qs = procurement_qs.filter(
                order_date__gte=datetime.strptime(date_from, '%Y-%m-%d').date()
            )
        except ValueError:
            date_from = ''
    if date_to:
        try:
            procurement_qs = procurement_qs.filter(
                order_date__lte=datetime.strptime(date_to, '%Y-%m-%d').date()
            )
        except ValueError:
            date_to = ''

    records = list(procurement_qs.order_by('-order_date'))

    active_value = sum(
        p.total_cost for p in records
        if p.status in (Procurement.Status.DRAFT, Procurement.Status.ORDERED)
    )
    received_value = sum(p.total_cost for p in records if p.status == Procurement.Status.RECEIVED)
    pending_delivery = sum(1 for p in records if p.status == Procurement.Status.ORDERED)

    status_counts = {choice.value: 0 for choice in Procurement.Status}
    for p in records:
        status_counts[p.status] = status_counts.get(p.status, 0) + 1

    return render(request, 'reports/procurement.html', {
        'procurements': records,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        'date_from': date_from,
        'date_to': date_to,
        'total_pos': len(records),
        'active_value': active_value,
        'received_value': received_value,
        'pending_delivery': pending_delivery,
        'chart': _procurement_status_chart(status_counts),
        'user_branch': user_branch,
        'active_page': 'reports',
    })
