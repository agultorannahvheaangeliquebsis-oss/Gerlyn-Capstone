import csv
import math

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Min, Sum
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
from core.timebounds import (
    local_day_bounds, local_month_bounds, local_range_bounds,
    parse_iso_date, range_from_request, resolve_date_range,
)


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


# One hue for all four KPI series. Procurement Cost and Total Transactions
# used to be stroked --ink-400, the muted non-text grey, so they rendered as if
# disabled rather than secondary. Defined once so the four cannot drift apart.
KPI_LINE = 'var(--brand)'
KPI_AREA = 'var(--green-50)'

# Primary/secondary tier, expressed as weight rather than colour.
#
# Not stroke opacity: --brand is 3.38:1 on --surface, only just over the 3:1
# SC 1.4.11 floor for a meaningful graphic, so ANY fade fails it — 0.85 drops
# to 2.76:1 and 0.5 to 1.78:1, the same washed-out value that made the old
# --green-300 focus ring invisible. Fading the line is how you get back to
# "disabled" by a different route.
#
# So the two dials are split by what each can afford to lose:
#   stroke-width  carries the line   — full-opacity hue, contrast untouched
#   fill-opacity  carries the area   — purely decorative, free to lighten
# Same ink, less weight: the chart equivalent of 400 vs 600 in one colour.
#
# Two dials, not three. Point markers stay a uniform r=3 across both tiers:
# they carry the per-point tooltip, so they are the interactive target, and
# shrinking them on the secondary metrics costs hit area to move the
# marker-to-line ratio from 4:1 to 3.33:1 — not a difference anyone sees.
KPI_TIERS = {
    'primary':   {'stroke_width': '2.5', 'area_opacity': '1'},
    'secondary': {'stroke_width': '1.5', 'area_opacity': '0.45'},
}
KPI_POINT_RADIUS = 3


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
            'tier': 'primary',
            'value_display': f'₱{totals["sales"]:,.2f}',
            'foot': f'{period_days} day{_plural(period_days)} · {txn_count} transaction{_plural(txn_count)}',
            'delta': _delta_pill(totals['sales'], prev['sales'], compare_label),
            'values': [float(d['sales']) for d in daily],
            'labels': every_day_labels,
            'x_fractions': None,
        },
        {
            'key': 'avg_transaction_value', 'label': 'Avg. Transaction Value',
            'unit': 'currency', 'tier': 'primary',
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
            'tier': 'secondary',
            'value_display': f'₱{totals["procurement_cost"]:,.2f}',
            'foot': f'{period_days} day{_plural(period_days)} of supplier spend',
            'delta': _delta_pill(totals['procurement_cost'], prev['procurement_cost'], compare_label),
            'values': [float(d['procurement_cost']) for d in daily],
            'labels': every_day_labels,
            'x_fractions': None,
        },
        {
            'key': 'total_transactions', 'label': 'Total Transactions', 'unit': 'count',
            'tier': 'secondary',
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
        tier = KPI_TIERS[spec['tier']]
        graphs[spec['key']] = {
            'label': spec['label'], 'unit': spec['unit'],
            'stroke': KPI_LINE, 'fill': KPI_AREA,
            'tier': spec['tier'],
            'strokeWidth': tier['stroke_width'],
            'areaOpacity': tier['area_opacity'],
            'pointRadius': KPI_POINT_RADIUS,
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


# An order is "open" until it reaches a terminal status — Completed and
# Cancelled are the only two ends of the lifecycle. Shared by the Needs
# Attention "Unfulfilled Orders" count (§2b) and the Open Orders table (§2g)
# so the panel's count and the table below it can never disagree about what
# counts as open.
OPEN_ORDER_STATUSES = [
    SalesTransaction.OrderStatus.PENDING,
    SalesTransaction.OrderStatus.APPROVED,
    SalesTransaction.OrderStatus.PREPARING,
]

# A dashboard preview, not the full list (§2g).
OPEN_ORDERS_LIMIT = 8


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
      Pending Orders OPEN_ORDER_STATUSES, i.e. every order not yet Completed
                     or Cancelled ("unfulfilled") — the same set the §2g Open
                     Orders table below lists, sharing one constant.
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

    pending_orders = sales_qs.filter(order_status__in=OPEN_ORDER_STATUSES).count()

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


ACTIVITY_FEED_SIZE = 5


def _relative_time(when, now):
    """
    Compact "5m ago" label for an aware datetime (DESIGN.md §2f).

    Computed in Python, never in SQL. The CONVERT_TZ trap core/timebounds.py
    works around only bites date-PART lookups pushed down to MySQL (__date,
    __month); ORDER BY on a raw DateTimeField and reading the column back are
    untouched by it, and this subtraction happens well after the value is an
    aware Python datetime. So the feed needs no timebounds helper.

    Past a week it gives an absolute date, where "23d ago" has stopped being
    easier to read than "Jul 15".
    """
    seconds = int((now - when).total_seconds())
    if seconds < 60:
        # Also absorbs a small negative delta: auto_now_add stamps the row a
        # beat after `now` was read, and "-3s ago" would be nonsense.
        return 'just now'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h ago'
    days = hours // 24
    if days < 7:
        return f'{days}d ago'
    # Not strftime('%-d') — that flag doesn't exist on Windows.
    return f'{when:%b} {when.day}'


def _relative_day(day, today):
    """
    The same label for a source that only knows a DATE. Procurement.order_date
    is a plain DateField and the model carries no created_at, so there is no
    time of day to be relative to — this says "Today" rather than inventing an
    hour so it can print "6h ago".
    """
    delta = (today - day).days
    if delta <= 0:
        return 'Today'
    if delta == 1:
        return 'Yesterday'
    if delta < 7:
        return f'{delta}d ago'
    return f'{day:%b} {day.day}'


def _actor_name(user):
    """Display name for whoever a posting is attributed to, or None."""
    if user is None:
        return None
    return user.get_full_name() or user.username


def _recent_activity(sales_qs, procurement_qs, billing_qs, now, limit=ACTIVITY_FEED_SIZE):
    """
    The Recent Activity feed (DESIGN.md §2f) — the latest postings across
    Sales, Purchase Orders and Billing, merged newest-first.

    Deliberately NOT scoped to the Dashboard's date-range control, per §2f:
    this answers "what just happened", not "what happened in the selected
    window", so narrowing the KPI rail to Today must not empty the feed.
    That's also why it takes the *unfiltered* branch-scoped querysets rather
    than the range-filtered ones the KPI rail uses.

    All three querysets are injected already branch-scoped — this function
    does no scoping of its own, so there is exactly one place (the caller)
    where a branch leak could be introduced, and each source can be stubbed
    independently when testing the merge.

    Each source contributes its own newest `limit` rows and the three are
    merged in Python — three small indexed LIMIT queries, rather than a UNION
    across tables that share neither a timestamp column name nor a shape.

    Timestamps
    ----------
    Sales (transaction_date) and Billing (date_recorded) are auto_now_add
    DateTimeFields. Procurement is NOT: `order_date` is a plain DateField and
    there is no created_at anywhere on the model. Its sort key is therefore
    local midnight of that date — the earliest moment the row can prove it
    existed — so a PO is never floated above a timestamped posting from the
    same day on the strength of a time nobody recorded. Making it aware also
    matters mechanically: comparing a naive datetime against the aware ones
    from the other two sources raises TypeError under USE_TZ.

    Attribution
    -----------
    Sale -> staff, PO -> created_by, Billing -> confirmed_by. All three are
    nullable, and confirmed_by is only populated once a payment is confirmed,
    so a freshly recorded pending payment carries no attribution rather than
    borrowing a name from a field that doesn't mean "who posted this".
    """
    today = now.date()
    entries = []

    for sale in sales_qs.select_related('branch', 'staff').order_by('-transaction_date')[:limit]:
        entries.append({
            'kind': 'sale', 'kind_label': 'Sale', 'icon': 'cart',
            'title': sale.order_number or f'Transaction #{sale.pk}',
            'url': reverse('transaction_detail', args=[sale.pk]),
            'branch': sale.branch.branch_name if sale.branch else 'Unassigned',
            'amount': sale.total_amount,
            'when': sale.transaction_date,
            'when_label': _relative_time(sale.transaction_date, now),
            'actor': _actor_name(sale.staff),
        })

    for po in procurement_qs.select_related('branch', 'created_by').order_by('-order_date', '-id')[:limit]:
        # See docstring — DateField, so local midnight is the honest sort key.
        po_when, _ = local_day_bounds(po.order_date)
        entries.append({
            'kind': 'procurement', 'kind_label': 'PO', 'icon': 'truck',
            'title': po.po_number or f'Procurement #{po.pk}',
            'url': reverse('procurement_detail', args=[po.pk]),
            'branch': po.branch.branch_name if po.branch else 'Unassigned',
            'amount': po.total_cost,
            'when': po_when,
            'when_label': _relative_day(po.order_date, today),
            'actor': _actor_name(po.created_by),
        })

    billings = billing_qs.select_related(
        'transaction', 'transaction__branch', 'confirmed_by'
    ).order_by('-date_recorded')[:limit]
    for bill in billings:
        branch = bill.transaction.branch if bill.transaction else None
        entries.append({
            'kind': 'billing', 'kind_label': 'Bill', 'icon': 'peso',
            'title': f'Payment #{bill.pk}',
            'url': reverse('billing_detail', args=[bill.pk]),
            'branch': branch.branch_name if branch else 'Unassigned',
            'amount': bill.amount_due,
            'when': bill.date_recorded,
            'when_label': _relative_time(bill.date_recorded, now),
            'actor': _actor_name(bill.confirmed_by),
        })

    entries.sort(key=lambda e: e['when'], reverse=True)
    return entries[:limit]


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

    # --- Date-range-scoped KPI rail, graph panel and donut (DESIGN.md §2a/
    # §2c). All three read one _dashboard_metrics bundle for the resolved
    # range, so a card, the graph it opens, and the donut can never disagree.
    # The range comes from the shared §2e control in the page head; an absent
    # or malformed selection falls back to the session, then to the last 30
    # days. Recent Activity (§2f) is deliberately NOT scoped by it — it
    # answers "right now", not "over the selected window".
    range_start, range_end, active_range = range_from_request(request, today=today)

    # 'All time' hands back a None start — no lower bound — which the filtering
    # surfaces simply drop from their query. This view cannot: it pre-allocates
    # one bucket per day across the range and compares against a previous
    # window of equal length, so it needs a real lower edge. It resolves one
    # here from the earliest posting on record.
    #
    # This is the MIN() that §2e's resolver deliberately avoids, and the
    # distinction matters: the resolver skips it because filtering never needs
    # to know the earliest record, whereas a per-day chart cannot be drawn
    # without one. Two indexed aggregates, only on the 'all' branch.
    if range_start is None:
        first_sale = sales_qs.aggregate(m=Min('transaction_date'))['m']
        first_po = procurement_qs.aggregate(m=Min('order_date'))['m']
        candidates = [d for d in (
            timezone.localtime(first_sale).date() if first_sale else None,
            first_po,
        ) if d]
        # No postings at all: collapse to a single day rather than an
        # unbounded span, which would bucket from year zero.
        range_start = min(candidates) if candidates else range_end
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

    # Open Orders (DESIGN.md §2g) — the workflow panel, deliberately NOT a
    # "latest orders" list. Non-terminal statuses only, oldest first, so the
    # order that has been waiting longest leads. Recent sales are Recent
    # Activity's job (§2f); this answers "what still needs action", which is
    # why the two panels no longer overlap.
    open_orders = sales_qs.filter(
        order_status__in=OPEN_ORDER_STATUSES
    ).select_related('branch', 'customer').order_by('transaction_date')[:OPEN_ORDERS_LIMIT]

    # Recent Activity (DESIGN.md §2f). Handed the branch-scoped querysets, NOT
    # the range-filtered ones above — §2f pins this to "what just happened",
    # independent of whatever range the KPI rail is showing.
    #
    # BillingRecord has no branch of its own, so it scopes through the sale it
    # belongs to. Scoped off the same `user_branch` _scoped_querysets used for
    # the other two rather than via scope_queryset(): the two helpers disagree
    # for a staff account with no branch assigned (_scoped_querysets leaves the
    # queryset unfiltered, branch_ids_for_user returns [] and filters it to
    # nothing), and the three sources in one feed must at least agree with each
    # other about what the viewer can see.
    billing_qs = BillingRecord.objects.all()
    if user_branch:
        billing_qs = billing_qs.filter(transaction__branch=user_branch)
    recent_activity = _recent_activity(sales_qs, procurement_qs, billing_qs, now)

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
        # The Dashboard has no other filters to preserve when the range
        # changes, so the control carries nothing (DESIGN.md §2e).
        'daterange_carry': [],
        # low_stock_count still feeds the Restock Suggestions header; the four
        # quiet stat tiles it used to sit beside are gone (duplicated the Needs
        # Attention rows / were static catalog facts, not daily decisions).
        'low_stock_count': len(restock),
        'restock': restock[:6],
        'needs_attention': needs_attention,
        'open_orders': open_orders,
        'recent_activity': recent_activity,
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

    branch_filter = request.GET.get('branch', '')
    if branch_filter and show_branch_filter:
        sales_qs = sales_qs.filter(branch_id=branch_filter)
    else:
        branch_filter = ''

    # The shared §2e control replaces the old month dropdown. A legacy
    # ?month=YYYY-MM is aliased to that month's exact range inside
    # range_from_request, so bookmarked month links land on the same rows
    # rather than silently falling back to the default window.
    #
    # Range comparison, NOT __year=/__month= — those compile to CONVERT_TZ on
    # MySQL and silently match nothing here. See core/timebounds.
    range_start, range_end, active_range = range_from_request(request)
    _, period_end = local_range_bounds(range_end, range_end)
    sales_qs = sales_qs.filter(transaction_date__lt=period_end)
    # 'All time' resolves to a None start — no lower bound at all, rather than
    # a substituted sentinel date that would quietly clamp real history.
    if range_start is not None:
        period_start, _ = local_range_bounds(range_start, range_start)
        sales_qs = sales_qs.filter(transaction_date__gte=period_start)

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
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        # Branch must survive a range change, or picking a preset would
        # silently widen the report back to every branch (DESIGN.md §2e).
        'daterange_carry': [('branch', branch_filter)],
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


def _compact_peso(amount):
    """
    A peso figure short enough to sit inside the donut's centre hole.

    The hole is only (RADIUS - STROKE/2) * 2 = 94 user units across, so a
    full "₱1,234,567.89" would run out over the ring. Thousands stay exact
    and comma-separated (₱68,585); only millions and up abbreviate, where
    the trailing digits carry no decision value at a glance. Nothing is
    lost either way — the panel subtitle still prints the exact figure to
    two decimals, and this is the same number, tightened to fit.
    """
    value = float(amount or 0)

    # Compared on the ROUNDED value, not the raw one: 999,999.60 would print
    # as "1,000,000" — a glyph wider than the exact form is allowed to get —
    # so it belongs in the abbreviated branch despite being under a million.
    rounded = round(value)
    if rounded < 1_000_000:
        return f'₱{rounded:,.0f}'

    # Each unit is chosen by what it would actually PRINT at two decimals,
    # not by a raw threshold. Testing the raw value instead lets 999,999,999
    # fail the billion check, fall through to millions, and render the
    # self-contradicting "₱1000.00M".
    for divisor, suffix in ((1_000_000_000, 'B'), (1_000_000, 'M')):
        scaled = value / divisor
        if round(scaled, 2) >= 1:
            return f'₱{scaled:.2f}{suffix}'
    return f'₱{rounded:,.0f}'  # unreachable; keeps every path returning a string


def _sales_by_branch_chart(sales_qs):
    """
    "Sales by Branch" donut for the Reports Sales tab.

    Aggregates whatever `sales_qs` the Sales tab has already filtered (branch
    dropdown + §2e date range), so the donut always describes the same rows as
    the KPI cards and the transactions table beneath it.

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
        return {'total': 0, 'total_display': _compact_peso(0), 'segments': [],
                'circumference': 0, 'radius': DONUT_RADIUS,
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
        # The donut's headline figure: the amount the ring divides up, which
        # is what the panel subtitle already states. The branch count reads
        # as the secondary line beneath it — a donut's centre answers "how
        # much", and the ring and legend already answer "across how many".
        'total_display': _compact_peso(total),
        'segments': segments,
        'circumference': round(circumference, 2),
        'radius': DONUT_RADIUS,
        'stroke': DONUT_STROKE,
        'box': DONUT_BOX,
        'centre': DONUT_BOX / 2,
        # Real branch count for the donut's secondary label — the segment
        # count understates it once branches fold into "Other branches".
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

    # Shared §2e range, replacing the bare from/to pair. Half-open aware
    # bounds, NOT date_recorded__date__gte/__lte — `date_recorded` is a
    # DateTimeField, so those lookups go through CONVERT_TZ on MySQL and
    # silently match zero rows here, which once made both date inputs return
    # an empty report for every range. local_range_bounds keeps the end
    # inclusive of its whole day. See core.timebounds.
    range_start, range_end, active_range = range_from_request(request)
    _, period_end = local_range_bounds(range_end, range_end)
    records_qs = records_qs.filter(date_recorded__lt=period_end)
    # See the Sales tab: 'All time' means no lower bound, not a sentinel date.
    if range_start is not None:
        period_start, _ = local_range_bounds(range_start, range_start)
        records_qs = records_qs.filter(date_recorded__gte=period_start)

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
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        'daterange_carry': [('branch', branch_filter)],
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

    # Shared §2e range, replacing the bare from/to pair. `order_date` is a
    # plain DateField with no tz component, so it compares against the dates
    # directly — no local_range_bounds needed here (see core/timebounds).
    range_start, range_end, active_range = range_from_request(request)
    procurement_qs = procurement_qs.filter(order_date__lte=range_end)
    # See the Sales tab: 'All time' means no lower bound, not a sentinel date.
    if range_start is not None:
        procurement_qs = procurement_qs.filter(order_date__gte=range_start)

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
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        'daterange_carry': [('branch', branch_filter)],
        'total_pos': len(records),
        'active_value': active_value,
        'received_value': received_value,
        'pending_delivery': pending_delivery,
        'chart': _procurement_status_chart(status_counts),
        'user_branch': user_branch,
        'active_page': 'reports',
    })
