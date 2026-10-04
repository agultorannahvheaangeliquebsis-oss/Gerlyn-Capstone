import csv
import math

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Min, Sum
from django.http import HttpResponse
from django.urls import reverse
from django.utils.http import urlencode
from django.utils import timezone
from datetime import date, datetime, timedelta

from billing.models import BillingRecord

from sales.models import Refund, SalesItem, SalesTransaction
from inventory.models import Batch, Inventory, WarehouseStock
from inventory.alerts import EXPIRING_SOON_DAYS, _expiry_tier, alert_summary
from procurement.models import Procurement
from branches.models import Branch
from accounts.models import User
from core.scoping import branch_for_user, branch_ids_for_user, scope_queryset
from core.permissions import OWNER, BRANCH_MANAGER, STAFF, role_required
from core.timebounds import (
    local_day_bounds, local_month_bounds, local_range_bounds,
    parse_iso_date, range_from_request, range_problem, resolve_date_range,
)
from reports.services import (
    PIPELINE_ORDER_STATUSES, PROCUREMENT_COST_STATUSES, REVENUE_ORDER_STATUSES,
    gross_sales_total, low_stock_count, net_sales_total, pipeline_sales,
    pipeline_value_total, procurement_cost_total, procurement_return_totals,
    refund_value_total, revenue_sales,
    sales_range_totals, scoped_report_querysets, scoped_sales_in_range,
)


# One definition, in core.scoping. This module used to carry its own copy
# behind a bare `except Exception: return None`, so any error (a missing
# profile, a DB hiccup) became "no branch" — which every caller reads as
# "Owner, show everything".
#
# NOTE: returns None for an Owner AND for a Manager/Staff whose profile
# branch is NULL (on_delete=SET_NULL makes that reachable — delete a branch
# and its manager lands here). Never branch on it to decide whether to
# filter a queryset; use scope_queryset() / branch_ids_for_user(), which
# tell the two apart and fail CLOSED. It is fine for filename labels and
# form defaults, where None just means "ask which branch".
get_user_branch = branch_for_user


# Moved to reports/services.py (scoped_report_querysets) so
# reports/api_views.py can share the exact same branch-scoping rule instead
# of re-deriving its own — this name stays as the alias every call site in
# this module already uses.
_scoped_querysets = scoped_report_querysets


# --- Dashboard: Inventory summary + Sales Trend by Branch ----------------
# Brings the Inventory Report's headline numbers and a new multi-branch
# trend chart onto the Dashboard, so the Owner/Manager/Staff don't have to
# leave it to see them. Two different scoping rules apply here, both driven
# by operational_dashboard:
#   Reactive to the branch selector  — Total Products (Floor/Warehouse),
#     Inventory Value, Total Units, Low/Critical, Top Products. Fed
#     querysets already narrowed by _scoped_querysets' override_branch_id.
#   Always every branch the viewer can see, ignoring the selector — the
#     Inventory Value by Branch chart and the Sales Trend by Branch chart,
#     same rule the Sales-by-Branch donut already follows (see
#     _dashboard_branch_breakdown). Fed querysets scoped only by
#     branch_ids_for_user, never by the selector's override.

def _dashboard_product_counts(inventory_qs, warehouse_qs):
    """Distinct product counts for the Total Products (Floor) / (Warehouse)
    KPI cards. Deliberately two separate figures, not one combined count —
    a product sitting only in the warehouse and one only on the floor are
    both real facts worth seeing, and folding them into one number would
    hide exactly that gap.

    One formula serves both the "All Branches" and "one branch selected"
    cases: the queryset is already branch-filtered (or not) by the caller,
    so counting distinct product_id here naturally dedupes across branches
    when unfiltered, and narrows to one branch's own products when not.
    """
    floor_count = inventory_qs.values('product_id').distinct().count()
    warehouse_count = warehouse_qs.values('product_id').distinct().count()
    return floor_count, warehouse_count


def _dashboard_inventory_metrics(inventory_qs):
    """Inventory Value / Total Units / Low-Critical Items — the reactive
    trio next to Total Products, same figures the Inventory Report computes
    for its KPI row, computed here over whatever branch scope the caller's
    queryset already carries."""
    records = list(inventory_qs)
    total_value = sum(
        (_inventory_unit_cost(inv) * inv.quantity_on_hand for inv in records),
        0,
    )
    total_units = sum(inv.quantity_on_hand for inv in records)
    # The canonical live predicate (reports.services.low_stock_count), not
    # the stored Inventory.restock flag — see that function's docstring.
    low_critical_count = low_stock_count(inventory_qs)
    return total_value, total_units, low_critical_count


def _sales_trend_by_branch_chart(sales_qs, branches, start, end, active_range=None,
                                  width=760, height=220, pad_x=24, pad_y=24):
    """
    One smoothed line per branch, daily sales over [start, end], sharing a
    single Y-scale so the lines are actually comparable to each other —
    unlike the KPI rail's per-metric graph, which only ever needs to compare
    a series against itself. Same curve/axis treatment as the KPI rail
    (_smooth_curve_commands, _even_x_fractions, _kpi_day_axis_labels)
    applied per line. The KPI panel's area fill is only used when a
    single branch is drawn: 2+ branches' fills stacked simultaneously (this
    chart, unlike the KPI rail, shows every line at once) would shade over
    each other rather than read as one chart, so those stay lines-only.

    Always covers every branch the viewer can see (branch_ids_for_user),
    never the branch-selector's override — see the module note above and
    operational_dashboard. `sales_qs` must already reflect that scope;
    `branches` is the matching list of Branch objects to draw one line each
    for, in the same order every time so a branch's colour and legend
    position never shift between requests.

    `active_range` is the same dashboard-wide preset _dashboard_kpi_payload
    already branches on — only used here to pick weekday-vs-month-start axis
    TICKS (see _kpi_day_axis_labels); point x-POSITIONS are evenly spaced
    for every range.

    Reuses _dashboard_daily_series per branch — there are at most a handful
    of branches, so N small queries stays cheap, and it keeps this chart's
    bucketing identical to every other daily chart on the page rather than
    hand-rolling a second grouped query that could drift out of sync.
    """
    n_days = (end - start).days + 1
    day_labels = [(start + timedelta(n)).isoformat() for n in range(n_days)]

    series_by_branch = []
    all_values = []
    for branch in branches:
        daily = _dashboard_daily_series(
            sales_qs.filter(branch=branch), Procurement.objects.none(), start, end
        )
        values = [float(d['sales']) for d in daily]
        series_by_branch.append((branch.branch_name, values))
        all_values.extend(values)

    if not any(all_values):
        return {'lines': [], 'labels': day_labels, 'peak': '', 'points': [], 'axisLabels': []}

    lo, hi = min(all_values), max(all_values)
    span = hi - lo
    plot_width = width - 2 * pad_x
    baseline = height - pad_y
    # Days are spaced EVENLY across the whole plot width (first day flush
    # left, last flush right), one day = one equal step whatever the month —
    # see _even_x_fractions for why the old per-month lanes were dropped.
    x_fractions = _even_x_fractions(n_days)
    xs = [round(pad_x + f * plot_width, 1) for f in x_fractions]

    def y_for(v):
        if span == 0:
            # All-zero (or all-tied) across every branch belongs at the
            # baseline when that tied value is 0 — the same place a 0 lands
            # within any normal series — and only falls back to the plot's
            # midpoint for a flat but NON-zero tie, where there's no
            # baseline-vs-zero meaning to preserve. Mirrors _kpi_graph_series'
            # own y_for exactly.
            return baseline if lo == 0 else height / 2
        return pad_y + (1 - (v - lo) / span) * (height - 2 * pad_y)

    colours = [TREND_BRANCH_COLOURS[i % len(TREND_BRANCH_COLOURS)] for i in range(len(series_by_branch))]

    lines = []
    for (name, values), colour in zip(series_by_branch, colours):
        coords = [(x, round(y_for(v), 1)) for x, v in zip(xs, values)]
        curve_cmds = _smooth_curve_commands(coords, pad_y, baseline)
        line_path = f'M{coords[0][0]},{coords[0][1]} {curve_cmds}'
        lines.append({
            'name': name,
            'colour': colour,
            'linePath': line_path,
            # Area fill only when ONE branch is drawn (a manager's own
            # branch): then it matches the KPI graph above exactly. With 2+
            # lines the fills would stack over each other, so none get one.
            'areaPath': (
                f'{line_path} L{coords[-1][0]},{baseline} L{coords[0][0]},{baseline} Z'
                if len(series_by_branch) == 1 else ''
            ),
            'total_display': _compact_peso(sum(values)),
        })

    # One point per day, every branch's value at that day together — what
    # the continuous-hover tooltip (dashboard.js) reads to show all branches
    # at once under the cursor, the same "everything at this x" shape the
    # KPI rail's own dual-value (this period/prior period) tooltip already
    # uses, just generalized from 2 values to N branches.
    points = [
        {
            'x': xs[j],
            'date': day_labels[j],
            'values': [
                {'name': name, 'colour': colour, 'value': values[j]}
                for (name, values), colour in zip(series_by_branch, colours)
            ],
        }
        for j in range(n_days)
    ]

    # Weekday ticks for 'Last 7 days', month-start ticks otherwise — both on
    # the same evenly spaced x positions the points use.
    axis_labels = _kpi_day_axis_labels(day_labels, active_range, width=width, pad_x=pad_x)

    return {
        'lines': lines,
        'labels': day_labels,
        'peak': f'Peak day: {_compact_peso(hi)}',
        'points': points,
        'axisLabels': axis_labels,
    }


# --- Dashboard KPI graph geometry --------------------------------------
# `SalesTransaction.transaction_date` is a DateTimeField, so day-bucketing
# happens in Python (not TruncDate/`.dates()`) — MySQL's CONVERT_TZ returns
# NULL under USE_TZ when the server has no zoneinfo tables loaded, same
# constraint already documented on `month_keys` in reports_dashboard below.
# `Procurement.order_date` is a plain DateField with no tz component, so it
# doesn't need this and is bucketed directly (see _dashboard_daily_series).

def _smooth_curve_commands(coords, y_min=None, y_max=None):
    """
    SVG path 'C' commands drawing a smooth, MONOTONE curve through `coords`,
    in order — assumes the cursor is already at coords[0] (an 'M' or 'L'
    placed it there) and emits only the segments after that.

    Monotone cubic interpolation (the Fritsch-Carlson construction, in the
    form d3's curveMonotoneX uses): between two consecutive points the curve
    never leaves the band their two y values span. So it can never dip below
    a flat run of zero days or rise above the real peak it is drawn through —
    the overshoot/undershoot that Catmull-Rom splines produce around a sharp
    one-day spike (and that an earlier version of this function patched with
    control-point clamps) cannot happen here by construction. A flat run
    stays exactly flat; a real maximum or minimum is a smooth top/bottom, not
    a cusp.

    Every segment is drawn the same way: there is no straight-line fallback
    for wide gaps any more. Monotone interpolation does not swoop across a
    long gap the way Catmull-Rom did, so Avg. Transaction Value's omitted
    days are a gentle S-curve like everywhere else, and an evenly spaced
    series (every other caller) never has one to begin with.

    Tangents are computed in drawing space (x, y as passed), so uneven x gaps
    are handled correctly. `y_min`/`y_max` are accepted for the callers'
    existing signature; as a last-resort guard each control point is still
    kept inside its own segment's endpoint band, which monotone tangents
    already guarantee.
    """
    n = len(coords)
    if n < 2:
        return ''
    if n == 2:
        # A straight line is the only monotone curve through two points.
        return f'L{coords[1][0]},{coords[1][1]}'

    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    slope = [(ys[i + 1] - ys[i]) / h[i] if h[i] else 0.0 for i in range(n - 1)]

    def sign(v):
        return -1.0 if v < 0 else 1.0

    tangent = [0.0] * n
    for i in range(1, n - 1):
        s0, s1 = slope[i - 1], slope[i]
        h0, h1 = h[i - 1], h[i]
        p = (s0 * h1 + s1 * h0) / (h0 + h1) if (h0 + h1) else 0.0
        tangent[i] = (sign(s0) + sign(s1)) * min(abs(s0), abs(s1), 0.5 * abs(p))
    # End tangents: the one-sided estimate, which (given interior tangents of
    # the same sign or zero) stays inside the monotone region.
    tangent[0] = (3 * slope[0] - tangent[1]) / 2 if h[0] else tangent[1]
    tangent[-1] = (3 * slope[-1] - tangent[-2]) / 2 if h[-1] else tangent[-2]

    segments = []
    for i in range(n - 1):
        x1, y1, x2, y2 = xs[i], ys[i], xs[i + 1], ys[i + 1]
        third = h[i] / 3
        cp1x, cp1y = x1 + third, y1 + tangent[i] * third
        cp2x, cp2y = x2 - third, y2 - tangent[i + 1] * third
        lo, hi = min(y1, y2), max(y1, y2)
        cp1y = min(max(cp1y, lo), hi)
        cp2y = min(max(cp2y, lo), hi)
        segments.append(f'C{cp1x:.1f},{cp1y:.1f} {cp2x:.1f},{cp2y:.1f} {x2},{y2}')
    return ' '.join(segments)


# Uppercase, mixed 3/4-letter abbreviations by explicit request — "SEPT",
# not "SEP", for September specifically; every other month stays 3 letters.
_KPI_AXIS_MONTH_ABBR = [
    'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
    'JUL', 'AUG', 'SEPT', 'OCT', 'NOV', 'DEC',
]


def _month_lanes(day_labels):
    """Distinct 'YYYY-MM' keys from day_labels, in first-seen (== calendar) order."""
    months = []
    seen = set()
    for label in day_labels:
        key = label[:7]
        if key not in seen:
            seen.add(key)
            months.append(key)
    return months


def _even_x_fractions(n):
    """0..1 x-position for each of `n` points spaced evenly from the plot's
    left edge to its right edge (first at 0, last at 1; a single point sits at
    the middle). One day = one equal step, whatever the range and whatever
    month a day falls in. Every daily dashboard chart uses this; it replaced
    an equal-width-lane-per-month mapping that gave a month's days a share of
    the width by month rather than by day count — a 26-day month and a 4-day
    one got half the plot each, so the short month's days were stretched,
    the long month's compressed, and the last day stopped short of the right
    edge. Evenly spaced days cannot land on top of each other at a month
    boundary either. Month names are labels only (see
    _kpi_axis_labels_month_starts); they never affect spacing."""
    if n < 1:
        return []
    return [i / (n - 1) for i in range(n)] if n > 1 else [0.5]


def _kpi_axis_labels_month_starts(day_labels, x_fractions, width=760, pad_x=24,
                                  max_labels=7):
    """
    X-axis ticks for ranges longer than a week: one label per distinct
    calendar month, placed at the x of that month's FIRST day present in
    `day_labels` (so a range starting Sep 5 labels SEPT at its left edge and
    OCT under Oct 1). Labels only — they never move a point; spacing comes
    from `x_fractions` alone. A range spanning more than `max_labels` months
    is thinned evenly in calendar order.

    Anchoring: the label at the very first day starts at its x and one at the
    very last day ends at its x (both would otherwise be clipped by the plot
    edge); every other label is centred on its x.
    """
    plot_width = width - 2 * pad_x
    firsts = []
    seen = set()
    for j, label in enumerate(day_labels):
        if label[:7] not in seen:
            seen.add(label[:7])
            firsts.append(j)
    if len(firsts) > max_labels:
        step = (len(firsts) - 1) / (max_labels - 1)
        firsts = sorted({firsts[round(i * step)] for i in range(max_labels)})
    last = len(day_labels) - 1
    labels = []
    for j in firsts:
        if last == 0:
            anchor = 'middle'      # a single-day range: its one label sits under its one point
        elif j == 0:
            anchor = 'start'
        elif j == last:
            anchor = 'end'
        else:
            anchor = 'middle'
        labels.append({
            'x': round(pad_x + x_fractions[j] * plot_width, 1),
            'text': _KPI_AXIS_MONTH_ABBR[int(day_labels[j][5:7]) - 1],
            'anchor': anchor,
        })
    return labels


def _kpi_day_axis_labels(day_labels, active_range, width=760, pad_x=24):
    """The x-axis ticks for any daily series, evenly spaced (see
    _even_x_fractions): a weekday label under every day for 'Last 7 days',
    month labels at each month's first day for everything longer."""
    x_fractions = _even_x_fractions(len(day_labels))
    if active_range == '7':
        return _kpi_axis_labels_weekday(day_labels, x_fractions, width=width, pad_x=pad_x)
    return _kpi_axis_labels_month_starts(day_labels, x_fractions, width=width, pad_x=pad_x)


def _kpi_axis_labels(day_labels, months, width=760, pad_x=24, max_labels=7):
    """
    FALLBACK x-axis ticks (month-lane boundaries) for a smoothed series that
    is handed no ticks of its own — in practice a single-day view. Every
    multi-day range gets _kpi_day_axis_labels instead, whose month ticks sit
    at each month's first day on the evenly spaced axis.

    X-axis tick labels: one tick per DISTINCT
    calendar month actually present in `day_labels`, in calendar order,
    never a repeat — "AUG" three times running because three sampled points
    happened to land in August is not three distinct labels to a reader,
    it's one label that looks broken (the bug an earlier, index/x-position-
    based ticking scheme kept producing).

    Positioned at each month's lane boundary, not at wherever its first
    real data point happens to fall — that is what makes consecutive ticks
    land exactly `plot_width / len(months)` apart. Capped to `max_labels` ticks, thinned by calendar order
    (never by raw index — see git history on the version this replaced, for
    why that matters once a series stops covering every day 1:1), only in
    the unlikely case a range spans more months than that.
    """
    n_months = len(months)
    if n_months == 0:
        return []

    plot_width = width - 2 * pad_x
    lane_width = plot_width / n_months

    indices = list(range(n_months))
    if n_months > max_labels:
        step = (n_months - 1) / (max_labels - 1)
        indices = sorted({round(i * step) for i in range(max_labels)})

    labels = []
    for pos, i in enumerate(indices):
        year, month = int(months[i][:4]), int(months[i][5:7])
        text = _KPI_AXIS_MONTH_ABBR[month - 1]
        x = round(pad_x + i * lane_width, 1)
        # Centering every label on its point clips the first/last one against
        # the viewBox edge (they sit right at pad_x from x=0/x=width) — the
        # end ticks anchor to their own inside edge instead so the text grows
        # inward, same convention as any axis with edge-flush ticks.
        if pos == 0 and len(indices) > 1:
            anchor = 'start'
        elif pos == len(indices) - 1 and len(indices) > 1:
            anchor = 'end'
        else:
            anchor = 'middle'
        labels.append({'x': x, 'text': text, 'anchor': anchor})
    return labels


# Fixed reference points on the Total Sales panel's 'today' x-axis, in
# ascending hour order. Only the ones that have actually elapsed are drawn
# (see _kpi_axis_labels_hourly) — 6PM on a chart viewed at 2PM would claim
# data exists three hours in the future.
_KPI_AXIS_HOUR_CANDIDATES = [(0, '12AM'), (6, '6AM'), (12, '12PM'), (18, '6PM')]


def _kpi_axis_labels_hourly(current_hour, width=760, pad_x=24):
    """
    X-axis tick labels for the Total Sales panel's 'today' view: whichever
    of 12AM/6AM/12PM/6PM have already elapsed, plus a final 'NOW' tick at
    the current hour — the hourly equivalent of _kpi_axis_labels' month
    ticks, for the single day _dashboard_hourly_series plots instead of a
    multi-month range.

    Positioned by the same hour/current_hour fraction _dashboard_kpi_payload
    uses for the line's own points, so a tick always lines up with the data
    directly below it. A candidate that lands exactly on the current hour
    is folded into the 'NOW' tick rather than drawn twice at the same x.

    Uppercase, matching every other axis-tick source on this panel
    (_KPI_AXIS_MONTH_ABBR, _KPI_AXIS_WEEKDAY_ABBR, and 12AM/6AM/12PM/6PM
    above) — there is no CSS text-transform doing this app-wide; each
    source is responsible for its own already-uppercase strings.
    """
    plot_width = width - 2 * pad_x

    def x_for(hour):
        if current_hour == 0:
            return pad_x
        return round(pad_x + (hour / current_hour) * plot_width, 1)

    ticks = [(h, text) for h, text in _KPI_AXIS_HOUR_CANDIDATES if h < current_hour]
    ticks.append((current_hour, 'NOW'))

    labels = []
    for pos, (hour, text) in enumerate(ticks):
        if pos == 0 and len(ticks) > 1:
            anchor = 'start'
        elif pos == len(ticks) - 1 and len(ticks) > 1:
            anchor = 'end'
        else:
            anchor = 'middle'
        labels.append({'x': x_for(hour), 'text': text, 'anchor': anchor})
    return labels


_KPI_AXIS_WEEKDAY_ABBR = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']  # date.weekday(): Mon=0


def _kpi_axis_labels_weekday(day_labels, x_fractions, width=760, pad_x=24):
    """
    X-axis tick labels for the Total Sales panel's 'Last 7 days' view: one
    tick per day, labeled MON/TUE/.../SUN (uppercase, matching every other
    axis-tick source on this panel — _KPI_AXIS_MONTH_ABBR, the hourly
    12AM/6AM/12PM/6PM/NOW ticks — there is no CSS text-transform doing this
    app-wide) — the month-lane ticks
    _kpi_axis_labels draws collapse to a single month abbreviation for any
    week that doesn't cross a month boundary (worse, still only one tick
    for a week that DOES cross one, mislabeling six of the seven days). A
    week is short enough that every day earns its own label, and a reader
    scanning one wants "which day", not "which month it's in".

    Reuses the x_fractions already computed for the line's own points
    (_even_x_fractions for 'Last 7 days') rather than deriving its own, so a
    tick always sits exactly under the day it labels. Every label is centred
    on its point — including the first and last — so the label centres are as
    evenly spaced as the days; pad_x leaves the end labels room to sit
    centred without clipping.
    """
    plot_width = width - 2 * pad_x
    labels = []
    for label, frac in zip(day_labels, x_fractions):
        text = _KPI_AXIS_WEEKDAY_ABBR[date.fromisoformat(label).weekday()]
        x = round(pad_x + frac * plot_width, 1)
        labels.append({'x': x, 'text': text, 'anchor': 'middle'})
    return labels


def _kpi_graph_series(daily_values, day_labels, width=760, height=220, pad_x=24, pad_y=24,
                      x_fractions=None, smooth=False, prev_values=None, axis_labels=None):
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

    'linePath' (an SVG <path> 'd' string) is always returned — straight
    'L' segments through the same coordinates as 'polyline' by default, or,
    with `smooth=True`, a monotone-smoothed curve through them instead
    (see _smooth_curve_commands — every segment is drawn the same way, and
    the curve never leaves the band its two end values span), with 'area'
    drawn from that same curve and 'axisLabels' populated. All four entries of the
    dashboard's swappable KPI panel pass smooth=True (_dashboard_kpi_payload)
    — the Sales Trend tab and the Procurement daily-spend chart are the only
    callers that still leave it False, and 'polyline'/'area'/'points' are
    byte-for-byte unaffected by anything added here for those callers.

    `axis_labels`, given, is used verbatim instead of the default fallback
    ticks below — the Total Sales panel's 'today' view passes its own hourly
    ticks (_kpi_axis_labels_hourly) here, since day_labels is a single day
    with nothing month-shaped to derive a tick from.

    `prev_values` (same length as `daily_values`, only ever passed alongside
    smooth=True) is the previous equal-length period's series, aligned by
    DAY-OFFSET-WITHIN-PERIOD rather than by calendar date — it reuses this
    call's own `xs`, so "3 days into the period" this time sits directly
    above "3 days into the period" last time, the same alignment the
    existing delta pills already compare on (_previous_window). It shares
    this call's y-scale (lo/hi widen to cover it below) rather than getting
    its own, since two lines on two different scales would look comparable
    while not being — only the resulting 'prevLinePath' is returned; no
    area, no axis labels, no points/tooltips of its own.
    """
    if not daily_values:
        return {
            'polyline': '', 'area': '', 'points': [], 'linePath': '',
            'axisLabels': [], 'smooth': smooth, 'prevLinePath': '',
        }

    lo, hi = min(daily_values), max(daily_values)
    if prev_values:
        lo = min(lo, min(prev_values))
        hi = max(hi, max(prev_values))
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
            # An all-zero flat series belongs at the baseline — the same
            # place a 0 lands within any normal (non-flat) series, not the
            # plot's vertical midpoint. The midline stays the fallback for a
            # flat NON-zero series (every day tied at the same real value),
            # where there is no baseline-vs-zero meaning to preserve.
            return baseline if lo == 0 else height / 2
        return pad_y + (1 - (v - lo) / span) * (height - 2 * pad_y)

    coords = [
        (round(x, 1), round(y_for(v), 1))
        for x, v in zip(xs, daily_values)
    ]
    points = [
        {
            'x': x, 'y': y, 'date': day_labels[i], 'value': daily_values[i],
            # The overlay's own value at this same day-offset-within-period
            # (see prev_values' docstring above) — None wherever there is no
            # overlay at all (every non-Total-Sales metric, and Total Sales
            # itself on 'All time'). Carried on the point rather than a
            # parallel array so the continuous-hover tooltip
            # (dashboard-kpi.js::showTooltip) can show both values for
            # whichever point the cursor is nearest, without a second
            # nearest-point lookup against a separately-indexed series.
            'prevValue': prev_values[i] if prev_values else None,
        }
        for i, (x, y) in enumerate(coords)
    ]
    polyline = ' '.join(f'{x},{y}' for x, y in coords)
    straight_cmds = ' '.join(f'L{x},{y}' for x, y in coords)
    area = f'M{coords[0][0]},{baseline} {straight_cmds} L{coords[-1][0]},{baseline} Z'

    # 'linePath' is always populated (straight commands when not smooth) —
    # the template/JS render the line element as a <path> either way, so an
    # empty 'linePath' on a non-smoothed entry would draw nothing at all
    # rather than falling back to anything.
    result = {
        'polyline': polyline,
        'area': area,
        'points': points,
        'linePath': f'M{coords[0][0]},{coords[0][1]} {straight_cmds}',
        'axisLabels': [],
        'smooth': smooth,
        'prevLinePath': '',
    }

    if smooth:
        curve_cmds = _smooth_curve_commands(coords, pad_y, baseline)
        result['linePath'] = f'M{coords[0][0]},{coords[0][1]} {curve_cmds}'
        result['area'] = (
            f'M{coords[0][0]},{baseline} L{coords[0][0]},{coords[0][1]} '
            f'{curve_cmds} L{coords[-1][0]},{baseline} Z'
        )
        result['axisLabels'] = (
            axis_labels if axis_labels is not None
            else _kpi_axis_labels(day_labels, _month_lanes(day_labels), width=width, pad_x=pad_x)
        )
        if prev_values:
            prev_coords = [
                (round(x, 1), round(y_for(v), 1))
                for x, v in zip(xs, prev_values)
            ]
            prev_curve_cmds = _smooth_curve_commands(prev_coords, pad_y, baseline)
            result['prevLinePath'] = f'M{prev_coords[0][0]},{prev_coords[0][1]} {prev_curve_cmds}'

    return result


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

    `sales_qs` is narrowed to realised revenue here (revenue_sales) so every
    surface built on this series — the KPI rail's Total Sales / Total
    Transactions / ATV, the Sales tab's Daily Sales panel, the per-branch
    trend chart and each dashboard's previous-window comparison — excludes
    Cancelled and still-open orders from one place, rather than each caller
    remembering to.

    `procurement_qs` is likewise narrowed to PROCUREMENT_COST_STATUSES (every
    status except Cancelled — see reports/services.py) so a cancelled PO
    can no longer inflate the Procurement Cost KPI/trend just for having
    once been raised.
    """
    sales_qs = revenue_sales(sales_qs)
    procurement_qs = procurement_qs.filter(status__in=PROCUREMENT_COST_STATUSES)

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


def _dashboard_hourly_series(sales_qs, day, up_to_hour):
    """
    Per-hour {hour, sales, txn_count} for `day`, hours 0..up_to_hour
    inclusive, zero-filled.

    The Total Sales panel's 'today' view only — a 1-day range has exactly
    one row from _dashboard_daily_series (nothing to plot a line through),
    so 'today' needs its own finer-grained series. `up_to_hour` is passed
    explicitly rather than derived from `day` itself so the 'yesterday'
    overlay call can be given the SAME hour span as 'today's — hour-of-day
    aligned, the same alignment _previous_window already uses for the
    day-level overlay on every other range, not a full 24 hours it could
    not be compared against index-for-index.
    """
    sales_qs = revenue_sales(sales_qs)
    buckets = {h: {'sales': 0, 'txn_count': 0} for h in range(up_to_hour + 1)}

    range_start, range_end = local_day_bounds(day)
    sales_rows = sales_qs.filter(
        transaction_date__gte=range_start, transaction_date__lt=range_end
    ).values_list('transaction_date', 'total_amount')
    for transaction_date, amount in sales_rows:
        hour = timezone.localtime(transaction_date).hour
        if hour in buckets:
            buckets[hour]['sales'] += amount or 0
            buckets[hour]['txn_count'] += 1

    return [{'hour': h, **buckets[h]} for h in range(up_to_hour + 1)]


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


def _previous_window(start, end):
    """
    The equal-length window immediately before [start, end], plus that
    window's length in days — the basis every delta pill compares against
    (DESIGN.md §2a).

    Extracted rather than repeated because the Dashboard rail and the
    Reports Sales tab both draw pills reading "vs previous N days": two
    surfaces each deriving their own offset is how the same sentence ends
    up meaning two different windows.
    """
    period_days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=period_days - 1), prev_end, period_days


def _dashboard_branch_breakdown(sales_qs, start, end, prev_start, prev_end):
    """
    The Sales-by-Branch donut + its legend's previous-window comparison,
    extracted out of _dashboard_metrics so it can also be called with a
    DIFFERENT sales_qs than the one driving the KPI rail — see
    operational_dashboard's branch selector: this donut is one of the charts
    that always compares every branch the viewer can see, never narrowed to
    whichever single branch the selector currently has picked, while the KPI
    rail itself does narrow. Behaviour for the KPI rail's own call is
    unchanged — this is a pure extraction, not a logic change.

    Both the current and previous window are narrowed to realised revenue
    (revenue_sales) so the donut, its centre figure and its legend's trend
    pills describe the same Completed-only total the KPI rail shows.
    """
    sales_qs = revenue_sales(sales_qs)

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

    return branch_breakdown, prev_branch_totals


def _procurement_net_adjustment(procurement_qs, start, end):
    """The supplier-return/credit deduction for one date-scoped slice of
    `procurement_qs` — same PROCUREMENT_COST_STATUSES narrowing
    _dashboard_daily_series applies, so this subtracts from exactly the POs
    that slice's gross figure already includes, nothing more.

    KATRINA-REPORT-001: the Dashboard's "Procurement Cost" KPI used to stay
    gross while the Procurement Report (and its API mirror) moved to Net —
    same label, two different numbers for the same scope. This is the one
    adjustment both now subtract, so the headline figures agree again.
    """
    scoped = procurement_qs.filter(
        status__in=PROCUREMENT_COST_STATUSES, order_date__gte=start, order_date__lte=end,
    )
    return procurement_return_totals(scoped)['net_adjustment']


def _dashboard_metrics(sales_qs, procurement_qs, start, end):
    """
    The single data source for the Dashboard's date-range-scoped surfaces
    (DESIGN.md §2a hero cards + graph, §2c donut): a daily series for the
    selected range, range totals for the KPI cards, per-day points for the
    Avg. Transaction Value chart line, the previous equal-length window's
    totals (for the hero cards' delta %), and sales grouped by branch.
    Everything those surfaces render is derived from this, not re-queried.
    """
    prev_start, prev_end, period_days = _previous_window(start, end)

    # prev_end is start - 1 day (see _previous_window), so [prev_start, end]
    # is one contiguous span. Fetched as a single _dashboard_daily_series
    # call and split by index rather than two separate calls per source (4
    # DB round trips -> 2): each round trip here costs far more than the
    # extra row volume does, so halving the count is the win.
    combined_daily = _dashboard_daily_series(sales_qs, procurement_qs, prev_start, end)
    prev_daily, daily = combined_daily[:period_days], combined_daily[period_days:]

    totals = _dashboard_range_totals(daily)
    atv_points = _dashboard_atv_points(daily)
    prev_totals = _dashboard_range_totals(prev_daily)

    # Net, not gross — see _procurement_net_adjustment. Subtracted from the
    # already-summed range total rather than threaded through the daily
    # series/trend line: a return's value is attributed to the scope it
    # belongs to for reporting purposes, not to a specific calendar day, so
    # there is no "which day" to re-bucket it into. The sparkline under the
    # KPI card therefore still traces the shape of gross day-by-day
    # spending; only the headline figure and its delta pill read Net.
    totals['procurement_cost'] -= _procurement_net_adjustment(procurement_qs, start, end)
    prev_totals['procurement_cost'] -= _procurement_net_adjustment(
        procurement_qs, prev_start, prev_end,
    )

    branch_breakdown, prev_branch_totals = _dashboard_branch_breakdown(
        sales_qs, start, end, prev_start, prev_end
    )

    # Whether the previous window holds anything at all is a property of the
    # RANGE, not of any one metric — when it is empty every KPI is equally
    # uncomparable. Resolved once here so the cards can state it once instead
    # of four cards each rendering the same "nothing to compare" pill.
    prev_window_empty = not any(
        prev_totals[k] for k in ('sales', 'procurement_cost', 'txn_count')
    )

    return {
        'daily': daily,
        # Exposed for the Total Sales panel's previous-period overlay line
        # (_dashboard_kpi_payload) — already computed above for the delta
        # pills' prev_totals, just not returned until now. Same length and
        # day-offset alignment as `daily` by construction (_previous_window
        # always returns an equal-length window), so the overlay can reuse
        # `daily`'s own x-coordinates index-for-index.
        'prev_daily': prev_daily,
        'totals': totals,
        'atv_points': atv_points,
        'prev_totals': prev_totals,
        'prev_window_empty': prev_window_empty,
        'period_days': period_days,
        'branch_breakdown': branch_breakdown,
        'prev_branch_totals': prev_branch_totals,
    }


def _percentage_change(current, previous):
    """Return the unmodified percentage change, or ``None`` for a zero baseline."""
    if float(previous) == 0:
        return None
    return ((float(current) - float(previous)) / float(previous)) * 100


def _format_percentage_change(pct):
    """Format a change for a KPI pill without altering the underlying value."""
    capped = abs(pct) > 100
    return {
        'display': '>100%' if capped else f'{abs(pct):.1f}%',
        'capped': capped,
        'tooltip': f'Actual change: {pct:+.1f}%' if capped else '',
    }


def _delta_pill(current, previous, compare_label, prev_window_empty=False,
                favorable_when='up'):
    """
    Period-over-period change as a status pill (DESIGN.md §2a) — the shared
    .pill-ok/.pill-danger/.pill-neutral trio, not a bespoke up/down/flat set.

    ``favorable_when`` defines the metric's business polarity: ``'up'`` for
    sales, transactions and average value, ``'down'`` for inverse metrics,
    and ``None`` when direction is informational (procurement cost without a
    budget). ``pct`` always retains the unrounded calculation. Only ``text``
    is capped, and ``tooltip`` exposes the rounded exact value when capped.

    A zero baseline never enters the percentage formula: positive current
    values are labelled New and two zero values are labelled No change.
    ``prev_window_empty`` is kept for call compatibility because the metric's
    own zero-baseline state, not unrelated activity in the window, decides
    what the card says.
    """
    pct = _percentage_change(current, previous)
    if pct is None:
        text = 'No change' if float(current) == 0 else (
            'New' if float(current) > 0 else 'No prior-period baseline'
        )
        return {
            'text': f'{text} vs {compare_label}', 'pill': 'pill-neutral',
            'pct': None, 'dir': None, 'capped': False, 'tooltip': '',
            'favorable': None,
        }
    if pct == 0:
        return {
            'text': f'No change vs {compare_label}', 'pill': 'pill-neutral',
            'pct': pct, 'dir': None, 'capped': False, 'tooltip': '',
            'favorable': None,
        }

    rising = pct > 0
    if favorable_when is None:
        favorable = None
    else:
        favorable = rising == (favorable_when == 'up')
    formatted = _format_percentage_change(pct)
    # `dir` rather than a ▲/▼ baked into `text`. The triangles were font
    # characters produced by the view, so no template could size or align them
    # and they rode high against the pill's 13px/1.6 text. reports/_delta_arrow
    # draws the direction instead, as the flex child .pill already has a gap
    # for, and `text` goes back to being a plain sentence a screen reader can
    # read without announcing a shape.
    return {
        'text': f'{formatted["display"]} vs {compare_label}',
        'pill': ('pill-neutral' if favorable is None else
                 'pill-ok' if favorable else 'pill-danger'),
        'pct': pct,
        'dir': 'up' if rising else 'down',
        'capped': formatted['capped'],
        'tooltip': formatted['tooltip'],
        'favorable': favorable,
    }


# How far an unfavorable change has to run before the whole KPI card takes a tint, and
# before that tint escalates. Both are existing modifiers — .kpi-card.warn and
# .kpi-card.danger, the same pair the stock and billing cards already use —
# so this grades an existing treatment rather than adding a new one.
#
# Deliberately NOT the pill's own threshold. Any unfavorable change turns its
# pill dangerous; that is one small status element. The tint repaints an
# entire card, and at 0.4%
# that reads as an incident rather than as the noise it is. So the tint stays
# off until a decline is worth a second look, and only reaches danger when it
# is steep enough to be the reason someone opened the report.
KPI_TINT_WARN_PCT = -2.0
KPI_TINT_DANGER_PCT = -10.0


def _delta_tint(delta):
    """
    The .kpi-card modifier a delta pill implies, or '' for no tint.

    Graded off the pill's own `pct` rather than a second comparison, so a
    card can never be tinted in a direction its pill contradicts. Favorable,
    neutral, and zero-baseline comparisons are untinted.
    """
    if not delta or delta.get('pct') is None or delta.get('favorable') is not False:
        return ''
    magnitude = abs(delta['pct'])
    if magnitude >= abs(KPI_TINT_DANGER_PCT):
        return 'danger'
    if magnitude >= abs(KPI_TINT_WARN_PCT):
        return 'warn'
    return ''


def _range_carry(active_range, range_start, range_end):
    """(name, value) pairs for the §2e range control, for the topbar branch
    switcher (partials/_branch_switch.html) to carry along — the reverse of
    _daterange.html's own daterange_carry, which carries branch INTO a range
    change. `date_from`/`date_to` only matter for a 'custom' range and `date`
    for a single 'day'; any other preset is fully named by `range` alone."""
    carry = [('range', active_range)]
    if active_range == 'day':
        carry.append(('date', range_end.strftime('%Y-%m-%d')))
    elif active_range == 'custom':
        carry += [
            ('date_from', range_start.strftime('%Y-%m-%d') if range_start else ''),
            ('date_to', range_end.strftime('%Y-%m-%d') if range_end else ''),
        ]
    return carry


def _kpi_delta(current, previous, compare_label, prev_window_empty,
               favorable_when='up'):
    """A delta pill carrying the card tint it implies, or None (see _delta_pill)."""
    delta = _delta_pill(
        current, previous, compare_label, prev_window_empty,
        favorable_when=favorable_when,
    )
    if delta:
        delta['tint'] = _delta_tint(delta)
    return delta


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
#
# The swappable KPI-rail panel (_dashboard_kpi_payload) puts all four of its
# own entries on 'primary' now, by explicit request — a side-by-side
# comparison read Procurement Cost/Total Transactions' 'secondary' weight as
# "broken" rather than "secondary" once they were switched into the same
# big single-panel view Total Sales/Avg. Transaction Value use, not the
# small always-visible card sparkline this split was designed for. 'secondary'
# stays defined rather than deleted — nothing calls it today, but it's a
# real, considered token (the accessibility math above), not dead guesswork,
# and the small-card context it fits could come back.
KPI_TIERS = {
    'primary':   {'stroke_width': '2.5', 'area_opacity': '1'},
    'secondary': {'stroke_width': '1.5', 'area_opacity': '0.45'},
}
KPI_POINT_RADIUS = 3


def _kpi_format(value, unit):
    return f'₱{value:,.2f}' if unit == 'currency' else f'{int(value):,}'


def _series_peak(values, labels, unit):
    """
    The series' high point, for the graph panel's subtitle.

    Replaces the foot line the panel used to carry, which was the selected
    card's foot repeated verbatim a few pixels below it. The peak is the one
    thing about the plotted line that nothing else on the page states: the
    chart has no y-axis, so without it the shape is readable but its
    magnitude is not.

    Two empty states, and they are not the same one:

      * no values at all — Avg. Transaction Value drops days with no
        transactions, so an inactive range leaves it with nothing to plot.
        The panel already renders its own "Nothing to chart" state for that,
        so this returns nothing and lets the chart do the talking.
      * values present but every one of them zero — the chart still draws,
        as a flat line on the midline, so something has to say what it is
        sitting at. "Peak ₱0.00" is technically true and completely useless.
    """
    if not values:
        return ''
    hi = max(values)
    if not hi:
        return 'No activity in this range'
    return f'Peak {_kpi_format(hi, unit)} · {labels[values.index(hi)]}'


def _range_label(active_range, start, end):
    """The §2e control's own preset text (templates/_daterange.html's active-
    range branch), mirrored here so the Total Sales panel's header line can
    be built server-side without duplicating this string in the template."""
    if active_range == 'today':
        return 'Today'
    if active_range == 'yesterday':
        return 'Yesterday'
    if active_range == '7':
        return 'Last 7 days'
    if active_range == 'all':
        return 'All time'
    if active_range == 'day':
        return f'{end:%Y-%m-%d}'
    if active_range == 'custom':
        return f'{start:%Y-%m-%d} – {end:%Y-%m-%d}'
    return 'Last 30 days'


def _kpi_metric_series(field, daily, prev_daily, every_day_labels, active_range,
                        sales_qs, now, start, supports_hourly):
    """
    (values, labels, x_fractions, axis_labels, prev_values) for one KPI's
    smoothed graph line — the by-granularity branch (hourly buckets for
    'today', evenly spaced days with weekday ticks for '7' and month-start
    ticks otherwise)
    Total Sales originally had, generalized to any metric that shares
    `daily`'s zero-filled-per-day shape: Total Sales' 'sales', Procurement
    Cost's 'procurement_cost', Total Transactions' 'txn_count'. NOT Avg.
    Transaction Value — its points intentionally omit inactive days rather
    than zero-fill (see _dashboard_atv_points), a different enough shape
    that _dashboard_kpi_payload builds it separately rather than forcing it
    through this same path.

    `supports_hourly` is False for Procurement Cost only: its order_date is
    a plain DateField with no time-of-day component (core/timebounds.py's
    own docstring), so there is no source data an hourly 'today' query
    could bucket — that metric's 'today' view falls back to the single
    zero-filled day _dashboard_daily_series already gives it (one point,
    flat at baseline per _kpi_graph_series' all-zero handling) rather than
    fabricating hours it has no data for.
    """
    if active_range == 'today' and supports_hourly and sales_qs is not None and now is not None:
        current_hour = now.hour
        today_hourly = _dashboard_hourly_series(sales_qs, start, current_hour)
        yesterday_hourly = _dashboard_hourly_series(
            sales_qs, start - timedelta(days=1), current_hour
        )

        def _hour_label(h):
            hour12 = h % 12 or 12
            return f'{hour12}:00 {"AM" if h < 12 else "PM"}'

        values = [float(h[field]) for h in today_hourly]
        labels = [_hour_label(h['hour']) for h in today_hourly]
        x_fractions = [
            (h['hour'] / current_hour) if current_hour else 0.0 for h in today_hourly
        ]
        axis_labels = _kpi_axis_labels_hourly(current_hour)
        prev_values = [float(h[field]) for h in yesterday_hourly]
        return values, labels, x_fractions, axis_labels, prev_values

    values = [float(d[field]) for d in daily]
    labels = every_day_labels
    # Every range: days evenly spaced edge to edge, one day = one step
    # (see _even_x_fractions).
    x_fractions = _even_x_fractions(len(every_day_labels))
    # 'Last 7 days' gets day-of-week ticks (one label per day — see
    # _kpi_axis_labels_weekday); every longer range and 'all' get month
    # labels at each month's first day (_kpi_axis_labels_month_starts).
    axis_labels = _kpi_day_axis_labels(every_day_labels, active_range)
    # Previous-period overlay values, only for a bounded range — see
    # _dashboard_kpi_payload's active_range docstring note. `prev_daily` is
    # index-aligned with `daily` (both period_days long), which is exactly
    # the alignment the overlay needs: same day-offset-within-period under
    # the same x, not same calendar date.
    prev_values = (
        [float(d[field]) for d in prev_daily] if active_range != 'all' else None
    )
    return values, labels, x_fractions, axis_labels, prev_values


def _dashboard_kpi_payload(metrics, start, end, active_range, sales_qs=None, now=None):
    """
    The four KPI cards (DESIGN.md §2a) and the graph series each one swaps
    into the shared panel, built together from a single metrics bundle.

    Built as one structure on purpose: §2a requires a card's headline figure
    and the graph panel's total for that same metric to agree exactly, so
    both read the identical value here rather than being computed twice.

    Values are cast to float because this is handed to the template through
    |json_script — Decimal is not JSON-serializable, and DjangoJSONEncoder
    would stringify it, leaving the client formatting "68585.00" as text.

    `active_range` gates the previous-period overlay line — Total Sales,
    Procurement Cost, and Total Transactions all share it (Avg. Transaction
    Value never gets one; see _kpi_metric_series' docstring for why): any
    BOUNDED preset ('today'/'7'/'30'/'custom') has a real equal-length
    "previous period" to compare against, but 'all' does not — its length
    grows every day, so there is no fixed span to look back by. Rather than
    overlay a window of some arbitrary length against a window with none,
    the overlay is skipped outright for 'all' and each panel reverts to the
    single solid line it had before this existed.

    `sales_qs`/`now` are only used on the 'today' branch, to run the hourly
    query _dashboard_daily_series can't answer (a 1-day range is exactly one
    row — see _dashboard_hourly_series). Both default to None so every other
    caller of this function (there are none today, but the signature staying
    callable without them matters) is unaffected.
    """
    totals, prev, daily = metrics['totals'], metrics['prev_totals'], metrics['daily']
    period_days = metrics['period_days']
    compare_label = f'previous {period_days} day{_plural(period_days)}'
    txn_count = totals['txn_count']
    prev_empty = metrics['prev_window_empty']

    # Retained as part of the payload contract. Zero baselines are now
    # described on their individual cards, so no rail-wide note is needed.
    compare_note = ''

    def x_fraction(day):
        return 0.0 if period_days <= 1 else (day - start).days / (period_days - 1)

    every_day_labels = [d['date'].isoformat() for d in daily]
    atv_points = metrics['atv_points']
    prev_daily = metrics['prev_daily']

    # The three metrics that share `daily`'s zero-filled-per-day shape all
    # go through the same by-granularity branch (_kpi_metric_series) —
    # Total Sales originally had this to itself; Procurement Cost and Total
    # Transactions get it here too now, sharing `daily`/`prev_daily`'s own
    # rows rather than a separate query (see this function's own module-
    # level rollout notes). Only Procurement Cost passes supports_hourly=
    # False — see _kpi_metric_series' docstring for why (no time-of-day
    # data to bucket).
    (total_sales_values, total_sales_labels, total_sales_x_fractions,
     total_sales_axis_labels, total_sales_prev_values) = _kpi_metric_series(
        'sales', daily, prev_daily, every_day_labels, active_range,
        sales_qs, now, start, supports_hourly=True,
    )
    (procurement_cost_values, procurement_cost_labels, procurement_cost_x_fractions,
     procurement_cost_axis_labels, procurement_cost_prev_values) = _kpi_metric_series(
        'procurement_cost', daily, prev_daily, every_day_labels, active_range,
        sales_qs, now, start, supports_hourly=False,
    )
    (total_transactions_values, total_transactions_labels, total_transactions_x_fractions,
     total_transactions_axis_labels, total_transactions_prev_values) = _kpi_metric_series(
        'txn_count', daily, prev_daily, every_day_labels, active_range,
        sales_qs, now, start, supports_hourly=True,
    )

    # Avg. Transaction Value stays on its own existing path — see this
    # function's rollout notes (and _kpi_metric_series' docstring) for why
    # its omit-inactive-days points can't go through the same zero-fill/
    # overlay machinery the other three now share. Axis TICKS are a
    # separate visual layer from the line's own (possibly sparser) points
    # though, so 'Last 7 days' still gets real weekday ticks here, built
    # from the FULL calendar week (every_day_labels) rather than ATV's own
    # active-only day list — a tick still lands under every day of the
    # week regardless of whether ATV has a point directly below it.
    atv_axis_labels = (
        _kpi_day_axis_labels(every_day_labels, active_range)
        if active_range != 'today' else None
    )

    # Plain-text line above each chart: the active range, plus what the
    # dashed overlay is compared against whenever one exists. Shared by the
    # three metrics that now carry a real overlay (Total Sales, Procurement
    # Cost, Total Transactions) — 'all' has no fixed-length previous window
    # to overlay for any of them, for the same reason across all three.
    # Avg. Transaction Value never has an overlay (see above), so it always
    # gets the plain range text with no legend.
    range_label = _range_label(active_range, start, end)
    if active_range == 'all':
        range_header, show_legend = range_label, False
    elif period_days == 1:
        # Only 'today' overlays *yesterday* (hourly); any other single day is
        # compared with the day before it, and saying "yesterday" of a chosen
        # 2026-09-01 would be wrong.
        before = 'yesterday' if active_range == 'today' else 'the day before'
        range_header, show_legend = f'{range_label} — overlaid with {before}', True
    else:
        range_header, show_legend = (
            f'{range_label} — overlaid with the {period_days} days before', True
        )

    specs = [
        {
            'key': 'total_sales', 'label': 'Total Sales', 'unit': 'currency',
            'tier': 'primary',
            'value_display': f'₱{totals["sales"]:,.2f}',
            'delta': _delta_pill(totals['sales'], prev['sales'], compare_label, prev_empty),
            'values': total_sales_values,
            'labels': total_sales_labels,
            'x_fractions': total_sales_x_fractions,
            'axis_labels': total_sales_axis_labels,
            'prev_values': total_sales_prev_values,
            'range_header': range_header,
            'show_legend': show_legend,
        },
        {
            'key': 'avg_transaction_value', 'label': 'Avg. Transaction Value',
            'unit': 'currency', 'tier': 'primary',
            'value_display': f'₱{totals["atv"]:,.2f}',
            'delta': _delta_pill(totals['atv'], prev['atv'], compare_label, prev_empty),
            # Days with no transactions are absent entirely, and the x
            # positions keep the remaining ones on the real time axis.
            'values': [float(p['value']) for p in atv_points],
            'labels': [p['date'].isoformat() for p in atv_points],
            'x_fractions': [x_fraction(p['date']) for p in atv_points],
            'axis_labels': atv_axis_labels, 'prev_values': None,
            'range_header': range_label, 'show_legend': False,
        },
        {
            # Label reads 'Net Procurement' (KATRINA-REPORT-001) — totals
            # and prev here are already net of supplier returns/credits
            # (see _dashboard_metrics/_procurement_net_adjustment), the
            # same definition the Procurement Report's own Net Procurement
            # card uses. The dict key stays 'procurement_cost' so the panel
            # toggle/template/JS wiring keyed on it needs no other change.
            'key': 'procurement_cost', 'label': 'Net Procurement', 'unit': 'currency',
            'tier': 'primary',
            'value_display': f'₱{totals["procurement_cost"]:,.2f}',
            'delta': _delta_pill(
                totals['procurement_cost'], prev['procurement_cost'], compare_label,
                prev_empty, favorable_when=None,
            ),
            'values': procurement_cost_values,
            'labels': procurement_cost_labels,
            'x_fractions': procurement_cost_x_fractions,
            'axis_labels': procurement_cost_axis_labels,
            'prev_values': procurement_cost_prev_values,
            'range_header': range_header, 'show_legend': show_legend,
        },
        {
            'key': 'total_transactions', 'label': 'Total Transactions', 'unit': 'count',
            'tier': 'primary',
            'value_display': f'{txn_count:,}',
            'delta': _delta_pill(txn_count, prev['txn_count'], compare_label, prev_empty),
            'values': total_transactions_values,
            'labels': total_transactions_labels,
            'x_fractions': total_transactions_x_fractions,
            'axis_labels': total_transactions_axis_labels,
            'prev_values': total_transactions_prev_values,
            'range_header': range_header, 'show_legend': show_legend,
        },
    ]

    cards, graphs = [], {}
    for spec in specs:
        # Every metric now gets a real smoothed curve, Avg. Transaction Value
        # included. ATV's points omit inactive days (see the spec above), so
        # consecutive points can sit very far apart in x (a real case: 14
        # points across three weeks, then a single point over 400 user-units
        # later with nothing between) — Catmull-Rom curving straight across
        # a gap that size once produced a wildly exaggerated overshoot right
        # at the transition. _smooth_curve_commands is now monotone, which
        # cannot overshoot, so every segment curves the same way (no straight
        # fallback) rather than disabling smoothing for the
        # whole series the way this used to — the earlier all-or-nothing
        # version traded the overshoot for a visibly straight/angular line
        # even where the data had no gap problem at all, which is its own
        # visual regression against Total Sales' curve. With the monotone
        # curve there is no per-segment fallback at all: every segment of
        # every metric is drawn the same way.
        graph = _kpi_graph_series(
            spec['values'], spec['labels'], x_fractions=spec['x_fractions'],
            # 260, not the 220 every other _kpi_graph_series caller still
            # gets by default — dashboard.html's #kpi-graph-svg's viewBox is
            # 760x260, taller than the two unrelated charts that also share
            # this function (Reports Sales tab, Procurement daily spend),
            # to fill the vertical space freed by dropping the label/value/
            # peak block for every KPI-rail panel's own head (kpi-graph-head).
            height=260,
            smooth=True,
            prev_values=spec['prev_values'],
            axis_labels=spec['axis_labels'],
        )
        cards.append({
            'key': spec['key'], 'label': spec['label'],
            'value_display': spec['value_display'],
            'delta': spec['delta'],
        })
        tier = KPI_TIERS[spec['tier']]
        # No 'delta' here: it was the selected card's own string repeated a few
        # pixels below it, and the card is tinted .active while its panel is
        # open. (There was a 'foot' too, until the KPI sub-texts were dropped
        # app-wide — cards are now label + value.) 'valueDisplay' does stay — it is
        # what the aria-live region announces on a card click, and a panel
        # that announced only its own title would answer nothing. 'peak' is
        # the replacement subtitle, and is the only line here that says
        # something the card does not.
        graphs[spec['key']] = {
            'label': spec['label'], 'unit': spec['unit'],
            'stroke': KPI_LINE, 'fill': KPI_AREA,
            'tier': spec['tier'],
            'strokeWidth': tier['stroke_width'],
            'areaOpacity': tier['area_opacity'],
            'pointRadius': KPI_POINT_RADIUS,
            'valueDisplay': spec['value_display'],
            'peak': _series_peak(spec['values'], spec['labels'], spec['unit']),
            # Header/legend row above the graph (DESIGN.md-style, see
            # templates/reports/dashboard.html's .kpi-graph-head) —
            # show_legend is True for whichever entries actually have a
            # rendered prevLinePath to label (Total Sales, Procurement
            # Cost, Total Transactions, on any bounded range); Avg.
            # Transaction Value never does.
            'rangeHeader': spec['range_header'],
            'showLegend': spec['show_legend'],
            # Drives the condensed panel UI (hidden title/value/peak block,
            # continuous-hover tooltip instead of per-point dots) in
            # dashboard-kpi.js — ALL FOUR KPI-rail panels get this,
            # independently of `graph['smooth']` above (all four are True
            # now, but this flag existed before that and is kept separate on
            # principle — the condensed UI and the curve are two different
            # decisions). dashboard-kpi.js used to gate this off entry.smooth
            # directly, which coupled "looks condensed" to "is curved" for no
            # real reason — see its own comments.
            'condensed': True,
            **graph,
        }
    return cards, graphs, compare_note


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

def _attention_tone(count, escalated=False):
    """
    Tone for a Needs Attention count badge: nothing outstanding is `ok`, an
    outstanding count that carries a hard failure (critical stock, already-
    expired batches) is `danger`, anything else is `warn`.

    A bare tone rather than a CSS class, because the template spends it on
    two different things — the .count-* badge and the .attn-card--danger
    escalation — and a view has no business naming either component.
    """
    if not count:
        return 'ok'
    return 'danger' if escalated else 'warn'


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


# --- What counts as revenue: one definition, every dashboard figure --------
# A sale is revenue only once its order_status reaches Completed. A Cancelled
# order is a voided/returned sale — the money was reversed — and a
# Pending/Approved/Preparing order has not moved stock or been paid for yet.
# Neither belongs in Total Sales, the daily trend, the Sales-by-Branch donut
# or the average transaction value.
#
# REVENUE_ORDER_STATUSES, PIPELINE_ORDER_STATUSES, revenue_sales() and
# pipeline_sales() now live in reports/services.py (imported at the top of
# this file) so reports/api_views.py reads the exact same definitions —
# this module used to carry its own copy, which is how the API and the
# template views drifted apart. PIPELINE_ORDER_STATUSES is deliberately the
# same non-terminal set the Open Orders panel above calls "open"
# (OPEN_ORDER_STATUSES): an order is in the pipeline for exactly as long as
# it is unfulfilled.


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
      Expiring Soon  inventory.alerts.expiry_alerts — floor/warehouse stock
                     expiring within EXPIRING_SOON_DAYS (30), plus any already
                     past expiry. Escalates to danger once something has
                     actually expired. The longer-horizon Approaching Expiry
                     tier (90 days) is an early-warning list, not surfaced on
                     this "needs attention now" panel.
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
    expired_count = summary['expired_count']
    expiring_count = expired_count + summary['expiring_soon_count']

    pending_pos = procurement_qs.filter(
        status__in=Procurement.OPEN_STATUSES
    ).count()

    pending_orders = sales_qs.filter(order_status__in=OPEN_ORDER_STATUSES).count()

    # See docstring — no due-date field exists to compute this against.
    overdue_billing = 0

    rows = [
        {
            'key': 'low_stock', 'icon': 'alert', 'label': 'Low Stock',
            'count': low_count,
            'tone': _attention_tone(low_count, escalated=summary['critical_count'] > 0),
            # Details are one clause each, no trailing qualifier: this is a
            # row's second line, not a paragraph. "across your branches" went
            # because the panel is already branch-scoped for whoever is
            # reading it, so it added a word and no fact.
            'detail': (
                f'{low_count} product{_plural(low_count)} at or below the reorder level.'
            ),
            'url': reverse('alerts_center') + '#low',
        },
        {
            'key': 'expiring', 'icon': 'clock', 'label': 'Expiring Soon',
            'count': expiring_count,
            'tone': _attention_tone(expiring_count, escalated=expired_count > 0),
            'detail': (
                f'{expiring_count} product{_plural(expiring_count)} expiring within '
                f'{EXPIRING_SOON_DAYS} days or already past expiry.'
            ),
            'url': reverse('alerts_center') + '#expiring',
        },
        {
            'key': 'pending_pos', 'icon': 'truck', 'label': 'Pending POs',
            'count': pending_pos,
            'tone': _attention_tone(pending_pos),
            'detail': (
                f'{pending_pos} purchase order{_plural(pending_pos)} raised but not yet received.'
            ),
            'url': reverse('procurement_list'),
        },
        {
            'key': 'pending_orders', 'icon': 'cart', 'label': 'Unfulfilled Orders',
            'count': pending_orders,
            'tone': _attention_tone(pending_orders),
            'detail': (
                f'{pending_orders} order{_plural(pending_orders)} still pending, approved or '
                f'preparing.'
            ),
            'url': reverse('transaction_list'),
        },
        {
            'key': 'overdue_billing', 'icon': 'peso', 'label': 'Overdue Billing',
            'count': overdue_billing,
            'tone': _attention_tone(overdue_billing),
            'detail': (
                f'{overdue_billing} billing record{_plural(overdue_billing)} past its due date.'
            ),
            'url': reverse('billing_list') + f'?status={BillingRecord.PaymentStatus.PENDING}',
        },
    ]

    actionable = [r for r in rows if r['count']]
    checks = len(rows)
    # Short enough to sit on one line beside "View all alerts →" at panel
    # width. "right now" is dropped from both: the panel is live either way,
    # and it was the half of the line nobody was reading.
    if actionable:
        needs = 'needs' if len(actionable) == 1 else 'need'
        subtitle = f'{len(actionable)} of {checks} checks {needs} action'
    else:
        subtitle = f'All {checks} checks clear'

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

    Row shape
    ---------
    kind_label is the row's whole subtitle, and it names the section of the
    app the posting came out of — "Sales", "Procurement", "Billing", the same
    words the sidebar uses for them. It is deliberately the only thing under
    the title: the branch and the posting staff used to follow it, and since
    attribution is nullable on all three sources (a pending payment has no
    confirmed_by yet) the line changed shape from row to row, which is what
    made a five-row feed tiring to read.

    Nothing here carries a branch, so no source select_relates one — every
    queryset arriving here is already scoped to what the viewer may see.
    """
    today = now.date()
    entries = []

    for sale in sales_qs.order_by('-transaction_date')[:limit]:
        entries.append({
            'kind': 'sale', 'kind_label': 'Sales', 'icon': 'cart',
            'title': sale.order_number or f'Transaction #{sale.pk}',
            'url': reverse('transaction_detail', args=[sale.pk]),
            'amount': sale.total_amount,
            'when': sale.transaction_date,
            'when_label': _relative_time(sale.transaction_date, now),
        })

    for po in procurement_qs.order_by('-order_date', '-id')[:limit]:
        # See docstring — DateField, so local midnight is the honest sort key.
        po_when, _ = local_day_bounds(po.order_date)
        entries.append({
            'kind': 'procurement', 'kind_label': 'Procurement', 'icon': 'truck',
            'title': po.po_number or f'Procurement #{po.pk}',
            'url': reverse('procurement_detail', args=[po.pk]),
            'amount': po.total_cost,
            'when': po_when,
            'when_label': _relative_day(po.order_date, today),
        })

    for bill in billing_qs.order_by('-date_recorded')[:limit]:
        entries.append({
            'kind': 'billing', 'kind_label': 'Billing', 'icon': 'peso',
            'title': f'Payment #{bill.pk}',
            'url': reverse('billing_detail', args=[bill.pk]),
            'amount': bill.amount_due,
            'when': bill.date_recorded,
            'when_label': _relative_time(bill.date_recorded, now),
        })

    entries.sort(key=lambda e: e['when'], reverse=True)
    return entries[:limit]


def _dashboard_greeting(now, user):
    """
    The Dashboard's <h1> — "Good morning, Marie" rather than a literal
    "Dashboard", which the <title>, the topbar section line and the active
    sidebar item already say three times over between them.

    Resolved server-side from timezone.localtime() so it agrees with every
    other time on this page: a client-side clock would happily say "Good
    morning" beside a feed row stamped "6h ago" for anyone whose device sits
    in another zone.

    Cut points are the ordinary ones — morning until noon, afternoon until
    6pm, evening after. First name only; the sidebar carries the full name
    and role two inches to the left, and a greeting is the one place in the
    app that should sound like a person talking.

    The exclamation mark is the warmth, and it is all of it. This line is read
    every time the store owner opens the app — several times a day, for years
    — so anything chattier than a greeting a colleague would actually say
    ("Hope you're having a great day!") curdles by the second week.
    """
    hour = now.hour
    if hour < 12:
        part = 'morning'
    elif hour < 18:
        part = 'afternoon'
    else:
        part = 'evening'
    # Accounts seeded without a first name fall back to the full name, then to
    # the username — never to a bare "Good morning," with a dangling comma.
    name = user.first_name.strip() or user.get_full_name().strip() or user.username
    return f'Good {part}, {name}!'


@login_required(login_url='accounts:login')
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

    # Branch selector (Owner only — Manager/Staff are already pinned to their
    # own branch everywhere in the app, so a selector would do nothing for
    # them). Picking a branch re-scopes every REACTIVE surface below to it,
    # exactly as if the Owner were that branch's own manager for as long as
    # it's selected — see _scoped_querysets. A fixed set of charts (Sales by
    # Branch, Inventory Value by Branch, Sales Trend by Branch) deliberately
    # ignore this and always compare every branch the viewer can see; those
    # are built later from `always_branch_ids`, never from `effective_branch`.
    show_branch_selector = request.user.role == User.Role.OWNER
    branch_filter = request.GET.get('branch', '') if show_branch_selector else ''
    # A non-numeric value (tampered URL) must not reach the ORM as a pk
    # lookup — Branch.objects.filter(pk='abc') raises ValueError rather than
    # returning no rows, since the pk field is numeric.
    if branch_filter and not branch_filter.isdigit():
        branch_filter = ''

    sales_qs, procurement_qs, inventory_qs, warehouse_qs, user_branch = _scoped_querysets(
        request.user, override_branch_id=branch_filter or None
    )

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
    kpi_cards, kpi_graphs, kpi_compare_note = _dashboard_kpi_payload(
        metrics, range_start, range_end, active_range, sales_qs=sales_qs, now=now
    )

    # `always_*` below never sees `branch_filter` — these three surfaces
    # (Sales by Branch, Inventory Value by Branch, Sales Trend by Branch)
    # always compare every branch the viewer's ROLE can see, regardless of
    # what the Owner's selector currently has picked. branch_ids_for_user is
    # the same pre-existing helper Alerts Center uses: None for Owner (every
    # branch), a Manager/Staff's own single branch otherwise.
    always_branch_ids = branch_ids_for_user(request.user)
    always_sales_qs = SalesTransaction.objects.all()
    always_inventory_qs = Inventory.objects.select_related('product', 'branch').prefetch_related('product__price_tiers').all()
    always_branches = Branch.objects.all()
    if always_branch_ids is not None:
        always_sales_qs = always_sales_qs.filter(branch_id__in=always_branch_ids)
        always_inventory_qs = always_inventory_qs.filter(branch_id__in=always_branch_ids)
        always_branches = always_branches.filter(id__in=always_branch_ids)
    always_branches = list(always_branches.order_by('branch_name'))

    prev_start, prev_end, _period_days = _previous_window(range_start, range_end)
    branch_breakdown, prev_branch_totals = _dashboard_branch_breakdown(
        always_sales_qs, range_start, range_end, prev_start, prev_end
    )
    branch_legend = _branch_legend_rows(branch_breakdown, prev_branch_totals)

    # No pre-filter by date here — _sales_trend_by_branch_chart calls
    # _dashboard_daily_series per branch, which already applies its own
    # local_range_bounds()-converted filter internally (see that function).
    sales_trend_by_branch = _sales_trend_by_branch_chart(
        always_sales_qs, always_branches, range_start, range_end, active_range=active_range
    )

    always_inventory_records = list(always_inventory_qs)
    for inv in always_inventory_records:
        inv.value = _inventory_unit_cost(inv) * inv.quantity_on_hand
    inventory_value_by_branch = _inventory_value_chart(
        _inventory_branch_summary(always_inventory_records)
    )

    # --- Inventory summary + Top Products (reactive to the branch selector,
    # same trio the Inventory/Sales Reports already compute — see the module
    # note above _dashboard_product_counts). Inventory is a snapshot (no date
    # axis, matching the Inventory Report tab); Top Products is range-scoped
    # like the KPI rail, so _top_products needs the same datetime conversion
    # _dashboard_branch_breakdown applies internally — done explicitly here
    # since _top_products, unlike _dashboard_daily_series, filters nothing
    # on its own.
    total_products_floor, total_products_warehouse = _dashboard_product_counts(
        inventory_qs, warehouse_qs
    )
    inventory_value, inventory_units, inventory_low_critical = _dashboard_inventory_metrics(
        inventory_qs
    )
    range_dt_start, range_dt_end = local_range_bounds(range_start, range_end)
    top_products = _top_products(
        revenue_sales(sales_qs).filter(
            transaction_date__gte=range_dt_start, transaction_date__lt=range_dt_end
        )
    )

    # --- Restock suggestions + "needs attention" list (same engine as the
    # Alerts Center — one alert_summary() call feeds both). Reactive: an
    # Owner viewing one branch sees just that branch's alerts, same as that
    # branch's own manager would.
    if user_branch:
        reactive_branch_ids = [user_branch.id]
    else:
        # No effective branch: an Owner with no selector pick sees every
        # branch (None); a Manager/Staff whose profile branch is NULL must
        # see nothing ([]) rather than inherit the Owner's all-branch view.
        reactive_branch_ids = branch_ids_for_user(request.user)
    summary = alert_summary(reactive_branch_ids)
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
    # belongs to. This used to scope off `user_branch` rather than via
    # scope_queryset(), on the reasoning that the two helpers disagreed for an
    # account with no branch assigned and the three sources in one feed had to
    # agree with each other. They no longer disagree: _scoped_querysets now
    # applies the same scope_queryset backstop, so all three sources fail
    # closed together and this feed is consistent the safe way round rather
    # than the leaky one.
    billing_qs = scope_queryset(
        BillingRecord.objects.all(), request.user, 'transaction__branch'
    )
    recent_activity = _recent_activity(sales_qs, procurement_qs, billing_qs, now)

    return render(request, 'reports/dashboard.html', {
        'greeting': _dashboard_greeting(now, request.user),
        # KPI rail + the graph panel they tab into + the Sales-by-Branch
        # donut, all derived from the one resolved range (DESIGN.md §2a/§2c).
        'kpi_cards': kpi_cards,
        'kpi_graphs': kpi_graphs,
        'kpi_compare_note': kpi_compare_note,
        # Default panel state (Total Sales) — server-rendered so the panel
        # holds real content before dashboard-kpi.js takes over on click.
        'default_kpi_key': 'total_sales',
        'default_kpi_graph': kpi_graphs['total_sales'],
        'branch_chart': branch_breakdown,
        'branch_legend': branch_legend,
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        # Changing the range must not silently drop the branch selection —
        # same reasoning as the Reports tabs' own daterange_carry.
        'daterange_carry': [('branch', branch_filter)],
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

        # --- Branch selector (Owner only) ---
        'show_branch_selector': show_branch_selector,
        'branch_filter': branch_filter,
        'branches': Branch.objects.all() if show_branch_selector else [],
        # The topbar branch chip renders this — same pattern as Floor Stock,
        # Reports, etc.
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        # The reverse of daterange_carry, for the topbar branch switcher
        # (partials/_branch_switch.html): the range must survive a branch
        # change too, or picking a branch would silently reset it to Last 30
        # days.
        'branch_switch_carry': _range_carry(active_range, range_start, range_end),

        # --- Inventory summary + Top Products (reactive) ---
        'total_products_floor': total_products_floor,
        'total_products_warehouse': total_products_warehouse,
        'inventory_value': inventory_value,
        'inventory_units': inventory_units,
        'inventory_low_critical': inventory_low_critical,
        'inventory_low_critical_display': f'{inventory_low_critical:,}',
        'top_products': top_products,

        # --- Always-every-branch comparison charts ---
        'inventory_value_by_branch': inventory_value_by_branch,
        'sales_trend_by_branch': sales_trend_by_branch,
    })


def _daily_sales_series(sales_qs, start, end):
    """
    Daily sales, for the Sales tab's trend panel (§2c's middle panel).

    Assembly only — the aggregation is _dashboard_daily_series and the
    geometry is _kpi_graph_series, the same two the Dashboard trend and the
    Procurement tab's Daily Spend already run through, so the three read as
    one chart type rather than three lookalikes. The peak line comes from
    _series_peak for the same reason: a sparkline has no y-axis, so without
    it the shape is readable but its magnitude is not.

    Procurement is passed an empty queryset rather than the helper being
    split in two. This page has no procurement column to draw and never
    will — §2c gives the Sales tab a sales breakdown, not a spend one — so
    the second series is unused here, and `.none()` costs no query at all
    (Django short-circuits an EmptyQuerySet before it reaches the database).

    `start` must already be a real date. 'All time' (§2e) resolves to None
    by design, and a per-day chart cannot bucket without a lower edge —
    the caller resolves one, the same exception the Dashboard makes.
    """
    daily = _dashboard_daily_series(sales_qs, Procurement.objects.none(), start, end)
    values = [float(d['sales']) for d in daily]
    labels = [d['date'].isoformat() for d in daily]
    series = _kpi_graph_series(values, labels)
    series['peak'] = _series_peak(values, labels, 'currency')
    return series


@role_required(OWNER, BRANCH_MANAGER, STAFF)
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

    # Branch-scoped but NOT range-scoped, kept for the previous-window
    # comparison below. That window lies entirely before the selected range,
    # so it has to be measured against this queryset: asking the range-filtered
    # one for rows before its own lower bound returns zero every time, which
    # renders as "nothing last period" rather than as the mistake it is.
    scoped_qs = sales_qs

    _, period_end = local_range_bounds(range_end, range_end)
    sales_qs = sales_qs.filter(transaction_date__lt=period_end)
    # 'All time' resolves to a None start — no lower bound at all, rather than
    # a substituted sentinel date that would quietly clamp real history.
    if range_start is not None:
        period_start, _ = local_range_bounds(range_start, range_start)
        sales_qs = sales_qs.filter(transaction_date__gte=period_start)

    sales_qs = sales_qs.order_by('-transaction_date')

    # `sales_qs` stays the full row set — the transactions table below lists
    # every order, Cancelled ones included, so they can be seen and audited.
    # Every *money* figure reads `revenue_qs` instead: realised revenue only
    # (Completed), the same definition billing_report already uses. Before
    # this, a voided P300 sale still showed as P300 / 1 order / P300 ATV here
    # while the Billing tab correctly showed P0 on the same data.
    revenue_qs = revenue_sales(sales_qs)

    total_orders = revenue_qs.count()
    # Gross Sales (Completed orders, before refunds) vs. Net Sales (Gross
    # minus Refund Value) — see reports/services.py for the shared
    # definitions. `total_sales`, the headline KPI, is NET: a refunded order
    # is still "Completed" (a refund never changes order_status — see
    # sales.models.Refund) so gross_sales_total alone would keep counting
    # money that was handed back. avg_order_value stays on the GROSS figure
    # deliberately — it describes the size of a typical completed order, not
    # a figure blended with refunds that don't line up one-to-one with this
    # window's own orders.
    gross_sales = gross_sales_total(sales_qs)
    refund_value = refund_value_total(sales_qs)
    total_sales = gross_sales - refund_value
    avg_order_value = (gross_sales / total_orders) if total_orders else 0
    # Channel mix, not revenue — a pending online order is still an online
    # order — so this one stays on the full row set.
    online_orders = sales_qs.filter(
        transaction_type=SalesTransaction.TransactionType.ONLINE
    ).count()

    # Committed but unfulfilled: orders a customer has placed that have not
    # been completed or cancelled (Pending / Approved / Preparing). Shown as
    # its own line under the KPI rail, never folded into Total/Net Sales —
    # the Owner keeps sight of the pipeline without it inflating revenue.
    pipeline_qs = pipeline_sales(sales_qs)
    pipeline_total = pipeline_value_total(sales_qs)
    pipeline_count = pipeline_qs.count()

    # Donut panel — built from the same revenue_qs as the KPIs above, so
    # branch + range selections carry into it (DESIGN.md 2c) and it shows the
    # same Completed-only total.
    branch_chart = _sales_by_branch_chart(revenue_qs)

    # A one-branch donut is a full ring, and every part of the panel then
    # states the same number: the ring's only segment, the figure in its
    # centre, its single legend row, and the Total Sales card above it. Four
    # readings of one total, and the "by branch" question answered with the
    # only branch there is. So the panel earns its slot only when there is a
    # split to show, and Top Products takes it otherwise — same slot, same
    # question ("what is this money made of"), an answer that varies.
    #
    # Keyed off the rendered breakdown rather than Branch.objects.count(),
    # because a second branch that recorded nothing in this range still
    # produces a single-segment ring. An active branch filter is the same
    # case reached deliberately.
    show_branch_donut = not branch_filter and branch_chart['branch_count'] > 1
    top_products = _top_products(revenue_qs)

    # Sales by Category donut (DESIGN.md §2c names category as the other half
    # of the row's donut/breakdown panel, alongside branch). Same revenue_qs,
    # so it always agrees with the KPI cards, the other two donuts and the
    # table on which rows are "this report".
    category_chart = _sales_by_category_chart(revenue_qs)

    # --- §2c trend panel + §2a delta pills ------------------------------
    # Both need a real lower edge, and 'All time' has none by design. The
    # Dashboard resolves one from the earliest posting on record; this does
    # the same, off the report's own filtered rows so the chart starts where
    # the table does rather than at some branch the filter excluded. One
    # indexed aggregate, and only on the 'all' branch.
    chart_start = range_start
    if chart_start is None:
        first_sale = revenue_qs.aggregate(m=Min('transaction_date'))['m']
        # No sales at all: collapse to a single day rather than an unbounded
        # span, which would bucket from year zero.
        chart_start = timezone.localtime(first_sale).date() if first_sale else range_end
    sales_series = _daily_sales_series(revenue_qs, chart_start, range_end)

    # Deltas compare against the equal-length window immediately before the
    # selected one, measured off scoped_qs for the reason noted above. For
    # 'All time' that window sits entirely before the earliest sale, so it is
    # empty by construction and every pill resolves to None — kpi_compare_note
    # states the reason once instead of three cards repeating it.
    prev_start, prev_end, period_days = _previous_window(chart_start, range_end)
    prev = _dashboard_range_totals(
        _dashboard_daily_series(scoped_qs, Procurement.objects.none(), prev_start, prev_end)
    )
    compare_label = f'previous {period_days} day{_plural(period_days)}'
    prev_window_empty = not (prev['sales'] or prev['txn_count'])
    kpi_compare_note = ''

    return render(request, 'reports/reports_home.html', {
        'transactions': sales_qs,
        'branch_chart': branch_chart,
        'show_branch_donut': show_branch_donut,
        'top_products': top_products,
        'category_chart': category_chart,
        'sales_series': sales_series,
        'total_orders': total_orders,
        'total_orders_display': f'{total_orders:,}',
        'total_sales': total_sales,
        'total_sales_display': f'{total_sales:,.2f}',
        # Gross/Refund not currently rendered by reports_home.html (it only
        # ever showed one "Total Sales" figure) — carried in context so the
        # Net Sales figure above them is auditable, and for the web/API
        # parity tests. See reports/services.py's module docstring for the
        # definitions.
        'gross_sales': gross_sales,
        'refund_value': refund_value,
        'avg_order_value': avg_order_value,
        'avg_order_value_display': f'{avg_order_value:,.2f}',
        'online_orders': online_orders,
        'online_orders_display': f'{online_orders:,}',
        # Committed-but-unfulfilled pipeline (Pending / Approved / Preparing),
        # rendered as a quiet line under the rail — see the template.
        'pipeline_total': pipeline_total,
        'pipeline_count': pipeline_count,
        # Online Orders gets no pill: the daily series carries no channel
        # split, so comparing it would mean a second query for a fourth card
        # nobody asked to compare. Absent beats invented.
        'delta_sales': _kpi_delta(total_sales, prev['sales'], compare_label, prev_window_empty),
        'delta_orders': _kpi_delta(total_orders, prev['txn_count'], compare_label, prev_window_empty),
        'delta_aov': _kpi_delta(avg_order_value, prev['atv'], compare_label, prev_window_empty),
        'kpi_compare_note': kpi_compare_note,
        'chart_start': chart_start,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this, so an Owner picking a branch
        # in the toolbar moves the chip too instead of leaving it reading
        # "All Branches" over a report that is no longer all branches — same
        # pattern as Floor Stock, Product Master List, etc.
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        # Branch must survive a range change, or picking a preset would
        # silently widen the report back to every branch (DESIGN.md §2e).
        'daterange_carry': [('branch', branch_filter)],
        # The reverse of daterange_carry, for the topbar branch switcher
        # (partials/_branch_switch.html): the range must survive a branch
        # change too, or picking a branch would silently reset it to Last 30
        # days.
        'branch_switch_carry': _range_carry(active_range, range_start, range_end),
        # So Export CSV downloads the rows this page is showing, not everything.
        'export_query': urlencode(
            _range_carry(active_range, range_start, range_end)
            + ([('branch', branch_filter)] if branch_filter else [])
        ),
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

# Sales Trend by Branch line colours (see the --series-* note in gerlyn.css):
# green, yellow, orange, then blue, purple, red — every line solid.
# Deliberately NOT SALES_BRANCH_RAMP, whose three greens overlapping lines
# cannot be told apart by. A branch keeps its colour between requests because
# `branches` arrives in a fixed order.
TREND_BRANCH_COLOURS = [
    'var(--series-1)', 'var(--series-2)', 'var(--series-3)',
    'var(--series-4)', 'var(--series-5)', 'var(--series-6)',
]

# Shared by every donut on the tab: the overflow fold is "the rest", not a
# category, so it sits outside whichever ramp is in play.
DONUT_OTHER_COLOUR = 'var(--ink-400)'

# Donut geometry — a stroked circle with a dash pattern, so each segment is one
# <circle> and no arc-path maths is needed (a 100%-single-segment donut, which
# is what a one-branch store renders, stays a clean full ring).
DONUT_RADIUS = 60
DONUT_STROKE = 26
DONUT_BOX = 160
DONUT_GAP = 2.0  # surface-coloured gap between adjacent fills


def _donut(rows, ramp, other_label):
    """
    Ring segments + geometry for one donut, from `rows` of (name, value)
    already sorted biggest-first.

    Shared by Sales by Branch and Top Products so the two donuts sitting in
    the same panel row are the same chart drawn twice, not two lookalikes
    that drift apart the next time one of them is touched. What a caller
    supplies is the part that genuinely differs: its colour ramp, and what
    the overflow fold is called.

    Names past the ramp's length fold into one `other_label` segment rather
    than getting improvised colours. The fold has to swallow everything from
    limit-1 onwards (not just limit onwards), or the row sitting in the last
    named slot is dropped and the segments stop summing to 100%.
    """
    geometry = {
        'radius': DONUT_RADIUS, 'stroke': DONUT_STROKE,
        'box': DONUT_BOX, 'centre': DONUT_BOX / 2,
    }
    total = sum(value for _, value in rows)
    if not total:
        return {'total': 0, 'segments': [], 'circumference': 0,
                'overflow': 0, **geometry}

    limit = len(ramp)
    if len(rows) > limit:
        folded = rows[limit - 1:]
        head = rows[:limit - 1] + [(other_label, sum(v for _, v in folded))]
    else:
        folded = []
        head = list(rows)

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
            'colour': DONUT_OTHER_COLOUR if is_other else ramp[i],
            # "<dash> <rest>" + a negative offset walks each segment round the ring.
            'dasharray': f'{max(arc - gap, 0.5):.2f} {circumference - max(arc - gap, 0.5):.2f}',
            'dashoffset': f'{-cursor:.2f}',
        })
        cursor += arc

    return {
        'total': total,
        'segments': segments,
        'circumference': round(circumference, 2),
        'overflow': len(folded),
        **geometry,
    }


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
    chart = _donut(rows, SALES_BRANCH_RAMP, 'Other branches')
    return {
        **chart,
        # The donut's headline figure: the amount the ring divides up, which
        # is what the panel subtitle already states. The branch count reads
        # as the secondary line beneath it — a donut's centre answers "how
        # much", and the ring and legend already answer "across how many".
        'total_display': _compact_peso(chart['total']),
        # Real branch count for the donut's secondary label — the segment
        # count understates it once branches fold into "Other branches".
        'branch_count': len(rows) if chart['total'] else 0,
    }


# --- Top Products by revenue (Reports → Sales tab) ----------------------
# DESIGN.md Section 1 asks for a "Top-Selling Products list"; this draws that
# ranking as a donut, the same chart Sales by Branch uses one panel over.
#
# The trade the donut makes, stated plainly: a ring can only carry as many
# slices as the ramp has legible steps, so the panel now names the top four
# products and folds the rest into one segment, where the list it replaced
# ranked eight. What the ring buys back is the share — the leader's 86% and
# the tail's 2% are a shape you read rather than two numbers you compare —
# and no revenue leaves the panel, because the fold is a segment, not a
# cut-off: the slices still sum to the total the subtitle prints.
#
# Its own ramp rather than SALES_BRANCH_RAMP, which runs
# brand / green-300 / action — dark, pale, dark. That is fine for branches,
# where the legend states the rank numerically anyway, but this panel IS a
# ranking, so lightness should fall with it. This is the monotone ordering
# the branch ramp's note already names as the better scale, extended by one
# pale step for a fourth slot.
#
# Contrast vs the white .card surface, same validator thresholds as the
# branch ramp:
#   --action-700  #14603C  7.59:1  PASS
#   --action      #1E7A4F  5.31:1  PASS
#   --brand       #2E9E6B  3.38:1  PASS
#   --green-300   #9CCDB4  1.78:1  under the 2.0:1 ordinal light-end floor
# The pale fourth step is the same known miss the branch donut already
# carries in its second slot, and it lands on the smallest named slice.
# Identity is never colour alone here either: every legend row prints its
# name, its peso figure and its share.
TOP_PRODUCTS_RAMP = [
    'var(--action-700)', 'var(--action)', 'var(--brand)', 'var(--green-300)',
]
TOP_PRODUCTS_OTHER_LABEL = 'Other products'

# SalesItem.product is SET_NULL, so a deleted product leaves live revenue rows
# pointing at nothing. Those rows are real money and belong in the total, so
# they are labelled rather than filtered out — dropping them would leave the
# panel's own total short of the Total Sales card sitting a few pixels above
# it, with nothing on the page explaining the gap.
DELETED_PRODUCT_LABEL = 'Deleted product'


def _top_products(sales_qs):
    """
    Products ranked by revenue over whatever `sales_qs` the Sales tab has
    already filtered, as donut segments.

    Scoping is inherited, not re-derived: `transaction__in=sales_qs` puts the
    page's branch dropdown and §2e date range behind one subquery, so this
    panel counts exactly the transactions the KPI cards, donut and table
    count. In particular it applies no order_status filter of its own —
    whatever the cards currently include, this includes, because two panels
    on one page disagreeing about which orders are real is worse than either
    answer being arguable.

    Revenue is `Sum('subtotal')`, the stored column, rather than
    quantity × unit_price recomputed in SQL. SalesItem.save() writes subtotal
    as exactly that product, so the two agree, and summing a stored decimal
    keeps this a plain indexed aggregate.

    Grouping is by product_id, not by name: two catalog entries sharing a name
    are two products, and every row whose product is gone collapses into one
    labelled bucket instead of one bucket per deleted id.
    """
    rows = list(
        SalesItem.objects
        .filter(transaction__in=sales_qs)
        .values('product_id', 'product__product_name')
        .annotate(revenue=Sum('subtotal'))
        .order_by('-revenue')
    )
    # Rows with no revenue at all would be 0%-wide slices carrying a legend
    # row each, so they are dropped before the ring is built rather than
    # drawn as segments nobody can see or click.
    named = [
        (r['product__product_name'] or DELETED_PRODUCT_LABEL, r['revenue'])
        for r in rows if r['revenue']
    ]
    chart = _donut(named, TOP_PRODUCTS_RAMP, TOP_PRODUCTS_OTHER_LABEL)
    if not chart['total']:
        return {**chart, 'total_display': _compact_peso(0),
                'product_count': 0, 'subtitle': ''}

    # Same centre treatment as Sales by Branch: how much, then across how
    # many. The count is the real product count, which the segment count
    # understates the moment the tail folds.
    count = len(named)
    total = chart['total']
    subtitle = (
        f'Share of ₱{total:,.2f} · all {count} product{_plural(count)}'
        if not chart['overflow'] else
        f'Share of ₱{total:,.2f} · top {len(chart["segments"]) - 1} of {count} '
        f'products, rest grouped'
    )
    return {
        **chart,
        'total_display': _compact_peso(total),
        'product_count': count,
        'subtitle': subtitle,
    }


# --- Sales by Category (Reports → Sales tab) ------------------------------
# DESIGN.md §2c's multi-panel row spec names "the donut/breakdown (sales by
# branch or category)" as that row's first panel. This is the category half
# of that pairing, added as a third donut alongside Sales by Branch and Top
# Products rather than swapped in for either — branch answers "where", Top
# Products answers "which item", this answers "which category", three
# different cuts of the same revenue rather than the same cut three ways.
#
# Reuses TOP_PRODUCTS_RAMP rather than inventing a fourth green scale: this
# panel is a revenue ranking exactly like Top Products, so the same
# monotone dark-to-pale ordering (and the same accepted pale-fourth-slot
# trade-off, see that ramp's note above) applies for the same reason.
CATEGORY_OTHER_LABEL = 'Other categories'

# A line item lands here when it has no category to report at all: either its
# product was filed under none (Product.category is nullable) or the product
# itself is gone (SalesItem.product is SET_NULL, so the category join is
# unreachable). Both are indistinguishable from here, so they collapse into
# one bucket rather than one deleted-product bucket plus a separate
# no-category one.
UNCATEGORIZED_LABEL = 'Uncategorized'


def _sales_by_category_chart(sales_qs):
    """
    "Sales by Category" donut for the Reports Sales tab.

    Aggregates whatever `sales_qs` the Sales tab has already filtered (branch
    dropdown + §2e date range) — the same branch- and range-scoped queryset
    Sales by Branch and Top Products read, so all three donuts in the panel
    row, the KPI cards above them and the transactions table below always
    describe the same rows.

    Grouping is by category_id, not by name: two categories sharing a name
    stay two rows, and every line item whose product carries no category (or
    whose product is gone) collapses into one Uncategorized bucket instead of
    being dropped or splitting across several unlabelled ones.
    """
    rows = list(
        SalesItem.objects
        .filter(transaction__in=sales_qs)
        .values('product__category_id', 'product__category__category_name')
        .annotate(revenue=Sum('subtotal'))
        .order_by('-revenue')
    )
    # Same zero-revenue guard _top_products uses: a 0%-wide slice would still
    # cost the panel a legend row nobody can see.
    named = [
        (r['product__category__category_name'] or UNCATEGORIZED_LABEL, r['revenue'])
        for r in rows if r['revenue']
    ]
    chart = _donut(named, TOP_PRODUCTS_RAMP, CATEGORY_OTHER_LABEL)
    return {
        **chart,
        # Same centre treatment as the other two donuts in this row: peso
        # total first, category count second.
        'total_display': _compact_peso(chart['total']),
        'category_count': len(named) if chart['total'] else 0,
    }


def _inventory_unit_cost(inv):
    """Cost basis for one unit — the deepest bulk tier where one exists, else retail.

    The lowest price the store is willing to sell a unit at is the nearest
    stand-in for what it cost, which is the same approximation the old flat
    wholesale_price provided here. Callers list() a queryset that has
    prefetched product__price_tiers, so this costs no query per row.
    """
    if not inv.product:
        return 0
    return inv.product.bulk_floor_price or 0


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


# Trailing window a floor-stock row's own sales are measured against to call
# it "non-performing" — see _non_performing_products docstring for the full
# rule. Same 30-day figure as inventory.alerts.EXPIRING_SOON_DAYS: it is
# already the store's working definition of "needs attention soon" for
# expiry, and reusing it here means Reports doesn't introduce a second,
# unrelated time horizon for staff to learn. Documented in docs/METRICS.md.
NON_PERFORMING_SALES_WINDOW_DAYS = 30


def _non_performing_products(branch_ids=None):
    """
    Flag every floor-stock (Inventory) row that is non-performing,
    near-expiry, or both. Rows that are neither are not returned — same
    convention as low_stock_alerts()/expiry_alerts(), which only ever list
    what needs attention. Full rule, with rationale, lives in
    docs/METRICS.md; this is the one place it is computed.

    Non-Performing
      quantity_on_hand > 0 AND zero units sold (Completed orders only —
      the same REVENUE_ORDER_STATUSES revenue_sales() uses, so an order
      still Pending or a voided Cancelled sale can't count as "activity")
      in the trailing NON_PERFORMING_SALES_WINDOW_DAYS (30) days. Units are
      SalesItem.quantity_sold, not distinct transactions, so one big order
      still reads as real velocity.

      Zero stock is excluded on purpose: a product with nothing on the
      floor isn't "not selling", it's out of stock — that is already
      low_stock_alerts()'s job, and double-flagging it here would tell
      staff to do nothing about a shelf that actually needs restocking.

    Near-Expiry
      The nearest FLOOR batch's expiry tier (inventory.alerts._expiry_tier,
      the same function expiry_alerts() uses) is "Expiring Soon" (<=30
      days) or "Approaching Expiry" (<=90 days) — no new expiry rule, just
      this report reading the existing one. A row with no batch-tracked
      expiry at all is never near-expiry. "Expired" is deliberately NOT
      included: an already-expired lot's correct next step is the daily
      write-off sweep (see the Batch model's docstring), not a slot on a
      performance report — by the time it's expired the "should we still
      be stocking this" question is moot.

    A product can be both — slow to sell AND sitting on ageing stock is the
    strongest signal something needs a markdown/return/delisting decision,
    so those rows sort first.

    `branch_ids=None` -> all branches (Owner). Pass a list to scope to a
    manager/staff's branch (use core.scoping.branch_ids_for_user).
    """
    today = timezone.localdate()
    window_start = timezone.now() - timedelta(days=NON_PERFORMING_SALES_WINDOW_DAYS)

    inv_qs = (
        Inventory.objects.filter(quantity_on_hand__gt=0)
        .select_related('product', 'product__category', 'branch')
    )
    if branch_ids is not None:
        inv_qs = inv_qs.filter(branch_id__in=branch_ids)

    # Units actually sold per product/branch in the trailing window.
    sold_qs = SalesItem.objects.filter(
        transaction__order_status__in=REVENUE_ORDER_STATUSES,
        transaction__transaction_date__gte=window_start,
    )
    if branch_ids is not None:
        sold_qs = sold_qs.filter(transaction__branch_id__in=branch_ids)
    sold_totals = {
        (row['product_id'], row['transaction__branch_id']): row['units']
        for row in sold_qs.values('product_id', 'transaction__branch_id')
        .annotate(units=Sum('quantity_sold'))
    }

    # Nearest FLOOR batch expiry per product/branch — same source
    # expiry_alerts() reads, just grouped down to one date per row instead
    # of one row per lot, since this table is keyed by product/branch.
    batch_qs = Batch.objects.filter(
        location=Batch.Location.FLOOR, expiry_date__isnull=False, quantity__gt=0,
    )
    if branch_ids is not None:
        batch_qs = batch_qs.filter(branch_id__in=branch_ids)
    nearest_expiry = {
        (row['product_id'], row['branch_id']): row['nearest']
        for row in batch_qs.values('product_id', 'branch_id')
        .annotate(nearest=Min('expiry_date'))
    }

    rows = []
    for inv in inv_qs:
        key = (inv.product_id, inv.branch_id)
        units_sold = sold_totals.get(key, 0)
        is_non_performing = units_sold == 0

        expiry_date = nearest_expiry.get(key)
        days_until_expiry = None
        tier = None
        if expiry_date is not None:
            days_until_expiry = (expiry_date - today).days
            tier = _expiry_tier(days_until_expiry)
        is_near_expiry = tier in ('Expiring Soon', 'Approaching Expiry')

        if not (is_non_performing or is_near_expiry):
            continue

        if is_non_performing and is_near_expiry:
            status, pill_class, sort_rank = 'Non-Performing + Near-Expiry', 'pill-danger', 0
        elif is_non_performing:
            status, pill_class, sort_rank = 'Non-Performing', 'pill-warn', 1
        else:
            status, pill_class, sort_rank = 'Near-Expiry', 'pill-warn', 2

        rows.append({
            'product': inv.product,
            'branch': inv.branch,
            'quantity_on_hand': inv.quantity_on_hand,
            'expiry_date': expiry_date,
            'days_until_expiry': days_until_expiry,
            'units_sold': units_sold,
            'is_non_performing': is_non_performing,
            'is_near_expiry': is_near_expiry,
            'status': status,
            'pill_class': pill_class,
            '_sort_rank': sort_rank,
        })

    rows.sort(key=lambda r: (r['_sort_rank'], r['product'].product_name if r['product'] else ''))
    return rows


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
    Procurement.Status.APPROVED: '#3B6FD4',
    Procurement.Status.PENDING_OWNER_REVIEW: '#DB2777',
    Procurement.Status.PENDING_SUPPLIER_REVIEW: '#0891B2',
    Procurement.Status.TERMS_AGREED: '#CA8A04',
    Procurement.Status.INCOMING: '#7C3AED',
    Procurement.Status.PARTIALLY_RECEIVED: '#B45309',
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


def _billing_payment_breakdown(completed_records, total_revenue, refunds_by_transaction=None):
    """Revenue-by-payment-method rows for the Billing Report (completed
    orders only), net of any refund recorded against each row's own
    transaction — the same Net Sales definition `total_revenue` itself uses
    (see billing_report / reports/services.py). Netted per-row rather than
    once at the end so the rows still sum to `total_revenue` exactly and
    each pct below stays a real share of it, instead of the table quietly
    summing to more than the KPI above it once a refund exists.
    """
    refunds_by_transaction = refunds_by_transaction or {}
    by_method = {}
    for r in completed_records:
        row = by_method.setdefault(r.payment_method, {'count': 0, 'total': 0})
        row['count'] += 1
        row['total'] += r.amount_due - refunds_by_transaction.get(r.transaction_id, 0)

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

# Excel/Sheets treats a cell starting with one of these as a formula to
# evaluate rather than literal text — e.g. a customer-supplied reference
# number of =cmd|'/c calc'!A1 would execute for whoever opens the export.
_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@')


def _csv_safe(value):
    """Prefix a leading apostrophe onto a string starting with a
    formula-trigger character so spreadsheet software renders it as literal
    text instead of evaluating it — the standard CSV-injection mitigation.
    Only strings are touched; numeric cells (Decimal/int) pass through, so a
    genuinely negative amount or quantity is never mistaken for this."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


class _SafeCSVWriter:
    """Wraps csv.writer so every row written anywhere in export_csv is
    sanitized in one place, regardless of which `kind` branch built it —
    rather than remembering to sanitize each field across every branch."""

    def __init__(self, underlying):
        self._underlying = underlying

    def writerow(self, row):
        self._underlying.writerow(_csv_safe(v) for v in row)


@login_required(login_url='accounts:login')
def export_csv(request, kind):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to export reports.')
        return render(request, 'reports/no_access.html')

    user_branch = get_user_branch(request.user)
    today = timezone.localdate()

    # The export follows its report page's active date range when the link
    # names one (every report page's own Export CSV link does, via
    # export_query — see reports_dashboard/procurement_report/billing_
    # report). Only an EXPLICIT range narrows it: falling back to the
    # session's last selection, as the pages do, would quietly shrink a bulk
    # export a caller expected to be complete. Inventory has no range of its
    # own to follow — it is a live snapshot, same as the Inventory Report
    # tab (KATRINA-REPORT-002).
    explicit_range = None
    redirect_target = {
        'sales': 'sales_report', 'procurement': 'procurement_report', 'billing': 'billing_report',
    }.get(kind)
    if redirect_target and any(
        key in request.GET for key in ('range', 'date', 'date_from', 'date_to', 'month')
    ):
        problem = range_problem(request)
        if problem:
            messages.error(request, f'Could not export: {problem}')
            return redirect(redirect_target)
        explicit_range = range_from_request(request)[:2]
    sales_range = explicit_range if kind == 'sales' else None

    response = HttpResponse(content_type='text/csv')
    scope = user_branch.branch_name.replace(' ', '-') if user_branch else 'all-branches'
    response['Content-Disposition'] = (
        f'attachment; filename="gerlyn-{kind}-{scope}-{today}.csv"'
    )
    writer = _SafeCSVWriter(csv.writer(response))

    if kind == 'sales':
        if sales_range:
            # scoped_report_querysets fails closed too, and an Owner's `branch`
            # narrows the export the same way it narrows the page.
            qs = scoped_sales_in_range(
                request.user, *sales_range, branch_id=request.GET.get('branch') or None,
            )
        else:
            qs = SalesTransaction.objects.select_related('branch', 'staff', 'customer')
            # scope_queryset fails closed. `if user_branch:` did not, and this is a
            # bulk export: an unassigned Branch Manager got every branch's rows in
            # one download.
            qs = scope_queryset(qs, request.user, 'branch')
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
        qs = scope_queryset(qs, request.user, 'branch')  # fails closed — see 'sales'
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
        qs = Procurement.objects.select_related(
            'branch', 'supplier', 'supplier__supplier_profile'
        ).prefetch_related('items')  # actual_received_cost walks items.all() per row
        qs = scope_queryset(qs, request.user, 'branch')  # fails closed — see 'sales'
        if explicit_range:
            range_start, range_end = explicit_range
            # order_date is a plain DateField, same comparison
            # procurement_report itself uses — no local_range_bounds needed.
            qs = qs.filter(order_date__lte=range_end)
            if range_start is not None:
                qs = qs.filter(order_date__gte=range_start)
            branch_param = request.GET.get('branch')
            if branch_param and request.user.role == User.Role.OWNER:
                qs = qs.filter(branch_id=branch_param)
        writer.writerow([
            'PO Number', 'Branch', 'Supplier', 'Order Date', 'Delivery Date',
            'Status', 'Receipt Flag', 'Total Cost (PHP)', 'Actual Received Cost (PHP)',
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
                p.actual_received_cost,
            ])

    elif kind == 'billing':
        qs = BillingRecord.objects.select_related('transaction', 'transaction__branch')
        qs = scope_queryset(qs, request.user, 'transaction__branch')  # fails closed
        if explicit_range:
            range_start, range_end = explicit_range
            # date_recorded is a DateTimeField — half-open local bounds,
            # same reasoning billing_report itself documents (see
            # core.timebounds: a date_recorded__date lookup silently
            # matches nothing on this MySQL deployment).
            _, period_end = local_range_bounds(range_end, range_end)
            qs = qs.filter(date_recorded__lt=period_end)
            if range_start is not None:
                period_start, _ = local_range_bounds(range_start, range_start)
                qs = qs.filter(date_recorded__gte=period_start)
            branch_param = request.GET.get('branch')
            if branch_param and request.user.role == User.Role.OWNER:
                qs = qs.filter(transaction__branch_id=branch_param)
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


@login_required(login_url='accounts:login')
def billing_report(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
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
    gross_revenue = sum(r.amount_due for r in completed_records)
    # Net of refunds — a refund never changes order_status (see
    # sales.models.Refund), so a refunded order is still "Completed" and
    # still in completed_records; without this, a fully-refunded order kept
    # counting its original amount_due as revenue forever.
    refunds_by_transaction = dict(
        Refund.objects.filter(
            transaction_id__in=[r.transaction_id for r in completed_records],
            # Approved only — Pending/Rejected requests (P14) moved no money.
            status=Refund.Status.APPROVED,
        ).values('transaction_id').annotate(total=Sum('total_amount')).values_list('transaction_id', 'total')
    )
    refund_value = sum(refunds_by_transaction.values())
    total_revenue = gross_revenue - refund_value

    payment_breakdown = _billing_payment_breakdown(
        completed_records, total_revenue, refunds_by_transaction
    )

    return render(request, 'reports/billing_report.html', {
        'records': records,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — see reports_dashboard's
        # identical comment.
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        'daterange_carry': [('branch', branch_filter)],
        # The reverse of daterange_carry, for the topbar branch switcher
        # (partials/_branch_switch.html): the range must survive a branch
        # change too.
        'branch_switch_carry': _range_carry(active_range, range_start, range_end),
        'completed_count': len(completed_records),
        'total_revenue': total_revenue,
        # Not currently rendered by billing_report.html — carried for
        # auditability and the web/API parity tests, same as
        # reports_dashboard's gross_sales/refund_value.
        'gross_revenue': gross_revenue,
        'refund_value': refund_value,
        'collected': collected,
        'collected_count': len(collected_records),
        'outstanding': outstanding,
        'pending_count': len(pending_records),
        'payment_breakdown': payment_breakdown,
        'user_branch': user_branch,
        'active_page': 'reports',
        # KATRINA-REPORT-002: so Export CSV downloads the rows this page is
        # showing, not the branch's whole history — same pattern as
        # reports_dashboard's own export_query.
        'export_query': urlencode(
            _range_carry(active_range, range_start, range_end)
            + ([('branch', branch_filter)] if branch_filter else [])
        ),
    })


@login_required(login_url='accounts:login')
def sales_report(request):
    """
    The daily Sales Report: every transaction in the chosen day / date range
    (the shared §2e control — Today, Yesterday, Last 7 days, a single day or a
    custom range), with the range's totals printed above the list.

    Rows and totals both come from reports.services (scoped_sales_in_range /
    sales_range_totals), the same functions the JSON mirror at
    /api/reports/sales-report/ reads, so the two always agree.
    """
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'You do not have permission to view this report.')
        return render(request, 'reports/no_access.html')

    user_branch = get_user_branch(request.user)
    show_branch_filter = branch_ids_for_user(request.user) is None  # Owner only
    branch_filter = request.GET.get('branch', '') if show_branch_filter else ''

    # A range that cannot be honoured falls back to the last 30 days, and the
    # control then shows "Last 30 days" — say why rather than letting the
    # reader take those figures for the period they asked for.
    problem = range_problem(request)
    if problem:
        messages.warning(request, f'{problem} Showing the last 30 days instead.')
    range_start, range_end, active_range = range_from_request(request)

    # scope_queryset semantics live in scoped_report_querysets: fails CLOSED for
    # a Manager/Staff whose profile branch is NULL (an `if user_branch:` guard
    # showed them every branch). Owner-only `branch` is ignored for anyone else.
    transactions = scoped_sales_in_range(
        request.user, range_start, range_end, branch_id=branch_filter or None,
    )
    totals = sales_range_totals(transactions)

    return render(request, 'reports/sales_report.html', {
        'transactions': transactions,
        'totals': totals,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        'user_branch': user_branch,
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        'daterange_carry': [('branch', branch_filter)],
        'branch_switch_carry': _range_carry(active_range, range_start, range_end),
        # The export must download the rows on screen, so it carries the range
        # (and branch) the page resolved rather than the session's.
        'export_query': urlencode(
            _range_carry(active_range, range_start, range_end)
            + ([('branch', branch_filter)] if branch_filter else [])
        ),
    })


@login_required(login_url='accounts:login')
def inventory_report(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
        messages.error(request, 'You do not have permission to view this report.')
        return render(request, 'reports/no_access.html')

    user_branch = get_user_branch(request.user)
    branch_ids = branch_ids_for_user(request.user)
    show_branch_filter = branch_ids is None  # Owner only

    inventory_qs = Inventory.objects.select_related('product', 'branch').prefetch_related('product__price_tiers').all()
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
    # The canonical live predicate (reports.services.low_stock_count), not
    # the stored Inventory.restock flag — see that function's docstring.
    low_critical_count = low_stock_count(inventory_qs)
    product_lines = len({inv.product_id for inv in records if inv.product_id})

    branch_summary = _inventory_branch_summary(records)
    chart = _inventory_value_chart(branch_summary)

    # Same effective scope as `records` above: the account's own branch
    # access (branch_ids) narrowed further by whichever single branch an
    # Owner picked in the toolbar (branch_filter), so this table never
    # shows a branch the KPI cards/table above it don't.
    non_performing_branch_ids = [int(branch_filter)] if branch_filter else branch_ids
    non_performing_rows = _non_performing_products(non_performing_branch_ids)

    return render(request, 'reports/inventory_report.html', {
        'inventory_records': records,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — see reports_dashboard's
        # identical comment.
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        # No other filter on this tab, so nothing else to carry — see
        # reports_dashboard's identical comment for the ones that do.
        'branch_switch_carry': [],
        'total_value': total_value,
        'total_units': total_units,
        'low_critical_count': low_critical_count,
        'product_lines': product_lines,
        'branch_summary': branch_summary,
        'chart': chart,
        'non_performing_rows': non_performing_rows,
        'non_performing_window_days': NON_PERFORMING_SALES_WINDOW_DAYS,
        'user_branch': user_branch,
        'active_page': 'reports',
    })


def _daily_spend_series(records, start, end):
    """
    Daily procurement spend, for the Procurement tab's trend panel (§2c).

    Buckets in Python rather than in SQL because the caller has already
    pulled `records` into a list to compute the KPI totals — going back to
    the database for the same rows to group them would be a second query for
    data already in memory.

    Returns None when there is nothing to draw, so the template renders its
    empty state rather than an axis with no line on it.

    `start` is None on the "All time" range (§2e), which has no lower bound
    by design. A per-day chart cannot bucket without one, so it resolves the
    earliest order on record — the same exception, for the same reason, that
    the Dashboard makes.
    """
    if not records:
        return None
    lo = start or min(p.order_date for p in records)
    span = (end - lo).days + 1
    if span < 1:
        return None

    buckets = {lo + timedelta(days=i): 0 for i in range(span)}
    for p in records:
        if p.order_date in buckets:
            buckets[p.order_date] += p.total_cost

    days = sorted(buckets)
    values = [float(buckets[d]) for d in days]
    series = _kpi_graph_series(values, [d.isoformat() for d in days])
    hi = max(values)
    series['peak'] = (
        f'Peak ₱{hi:,.2f} · {days[values.index(hi)].isoformat()}' if hi
        else 'No spend in this range'
    )
    return series


@login_required(login_url='accounts:login')
def procurement_report(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF]:
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
    gross_procurement = procurement_cost_total(procurement_qs)
    return_totals = procurement_return_totals(procurement_qs)
    net_procurement = gross_procurement - return_totals['net_adjustment']

    active_value = sum(
        p.total_cost for p in records
        if p.status in Procurement.OPEN_STATUSES
    )
    received_value = sum(p.total_cost for p in records if p.status == Procurement.Status.RECEIVED)
    pending_delivery = sum(
        1 for p in records if p.status in (
            Procurement.Status.INCOMING,
            Procurement.Status.PARTIALLY_RECEIVED,
        )
    )

    status_counts = {choice.value: 0 for choice in Procurement.Status}
    for p in records:
        status_counts[p.status] = status_counts.get(p.status, 0) + 1

    return render(request, 'reports/procurement.html', {
        'procurements': records,
        'branches': Branch.objects.all() if show_branch_filter else [],
        'show_branch_filter': show_branch_filter,
        'branch_filter': branch_filter,
        # The topbar branch chip renders this — see reports_dashboard's
        # identical comment.
        'filter_branch': Branch.objects.filter(pk=branch_filter).first() if branch_filter else None,
        'range_start': range_start,
        'range_end': range_end,
        'active_range': active_range,
        'daterange_carry': [('branch', branch_filter)],
        # The reverse of daterange_carry, for the topbar branch switcher
        # (partials/_branch_switch.html): the range must survive a branch
        # change too.
        'branch_switch_carry': _range_carry(active_range, range_start, range_end),
        'total_pos': len(records),
        'active_value': active_value,
        'received_value': received_value,
        'pending_delivery': pending_delivery,
        'gross_procurement': gross_procurement,
        'supplier_return_value': return_totals['returned_goods'],
        'supplier_credit_value': return_totals['credits'],
        'net_procurement': net_procurement,
        'chart': _procurement_status_chart(status_counts),
        'spend_series': _daily_spend_series(records, range_start, range_end),
        # The third panel of §2c's row. active_value (Draft + Ordered) and
        # received_value are already computed above for the KPI cards, so the
        # meter is a second reading of numbers the page is showing anyway —
        # what it adds is the proportion between them, which no card states.
        'spend_split': {
            'received': received_value,
            'outstanding': active_value,
            'total': received_value + active_value,
            'pct': round(received_value / (received_value + active_value) * 100)
            if (received_value + active_value) else 0,
        },
        'user_branch': user_branch,
        'active_page': 'reports',
        # KATRINA-REPORT-002: so Export CSV downloads the rows this page is
        # showing, not the branch's whole history — same pattern as
        # reports_dashboard's own export_query.
        'export_query': urlencode(
            _range_carry(active_range, range_start, range_end)
            + ([('branch', branch_filter)] if branch_filter else [])
        ),
    })
