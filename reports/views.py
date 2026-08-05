import csv
import math

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone
from datetime import datetime, timedelta

from billing.models import BillingRecord

from sales.models import SalesTransaction
from inventory.models import Inventory, Product
from inventory.alerts import low_stock_alerts
from procurement.models import Procurement
from branches.models import Branch
from accounts.models import User
from core.scoping import branch_ids_for_user
from core.permissions import OWNER, BRANCH_MANAGER, role_required


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


# --- Dashboard hero-card sparklines ------------------------------------
# `SalesTransaction.transaction_date` is a DateTimeField, so day-bucketing
# happens in Python (not TruncDate/`.dates()`) — MySQL's CONVERT_TZ returns
# NULL under USE_TZ when the server has no zoneinfo tables loaded, same
# constraint already documented on `month_keys` in reports_dashboard above.
# `Procurement.order_date` is a plain DateField with no tz component, so it
# doesn't need this and is bucketed directly (see operational_dashboard).

def _daily_sales_series(qs, value_field=None, days=7):
    """
    Day-bucketed totals (or counts, if `value_field` is None) from
    `transaction_date` for the last `days` days, oldest first, zero-filled
    for days with no rows.
    """
    today = timezone.localtime().date()
    start = today - timedelta(days=days - 1)
    boundary = timezone.make_aware(datetime.combine(start, datetime.min.time()))
    buckets = {start + timedelta(n): 0 for n in range(days)}

    fields = ['transaction_date'] + ([value_field] if value_field else [])
    for row in qs.filter(transaction_date__gte=boundary).values_list(*fields):
        day = timezone.localtime(row[0]).date()
        if day in buckets:
            buckets[day] += (row[1] or 0) if value_field else 1

    return [buckets[start + timedelta(n)] for n in range(days)]


def _sales_period_delta_pct(qs, value_field=None, days=7):
    """% change between the last `days` days and the `days` before that."""
    end = timezone.localtime()
    start_current = end - timedelta(days=days)
    start_prev = start_current - timedelta(days=days)

    def _agg(gte, lt):
        filtered = qs.filter(transaction_date__gte=gte, transaction_date__lt=lt)
        if value_field:
            return filtered.aggregate(total=Sum(value_field))['total'] or 0
        return filtered.count()

    current, previous = _agg(start_current, end), _agg(start_prev, start_current)
    return round(((current - previous) / previous) * 100, 1) if previous else None


def _sparkline_points(series, width=64, height=24, pad=3):
    """
    Maps a numeric series onto an SVG polyline's `points` attribute, scaled
    into a width x height box, plus the trailing point for an end-dot. A
    flat series (all equal values) renders as a horizontal midline instead
    of dividing by zero.
    """
    if not series:
        return {'points': '', 'dot_x': 0, 'dot_y': height / 2}

    lo, hi = min(series), max(series)
    span = hi - lo
    n = len(series)
    step = width / (n - 1) if n > 1 else 0

    def y_for(v):
        if span == 0:
            return height / 2
        return pad + (1 - (v - lo) / span) * (height - 2 * pad)

    coords = [(round(i * step, 1), round(y_for(v), 1)) for i, v in enumerate(series)]
    dot_x, dot_y = coords[-1]
    return {'points': ' '.join(f'{x},{y}' for x, y in coords), 'dot_x': dot_x, 'dot_y': dot_y}


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

    sales_qs, procurement_qs, inventory_qs, user_branch = _scoped_querysets(request.user)

    now = timezone.localtime()
    today = now.date()

    # today_sales feeds _branch_status_rows below (each branch's share of
    # today's total), even though it's no longer its own hero card.
    today_sales = sales_qs.filter(
        transaction_date__date=today
    ).aggregate(total=Sum('total_amount'))['total'] or 0

    # --- Quiet strip ---
    pending_online = sales_qs.filter(
        transaction_type=SalesTransaction.TransactionType.ONLINE,
        order_status=SalesTransaction.OrderStatus.PENDING,
    ).count()

    # Inventory value at cost (wholesale price where known).
    inventory_value = sum(
        inv.quantity_on_hand * (inv.product.wholesale_price or inv.product.unit_price or 0)
        for inv in inventory_qs if inv.product
    )

    # --- Hero-row cards: Total Sales, Recent Sales, Procurement Cost,
    # Total Transactions (DESIGN.md Section 2, improvement #2/#2a) ---
    total_sales_alltime = sales_qs.aggregate(total=Sum('total_amount'))['total'] or 0
    total_sales_spark = _sparkline_points(_daily_sales_series(sales_qs, 'total_amount', days=7))
    total_sales_delta = _sales_period_delta_pct(sales_qs, 'total_amount', days=7)

    recent_30d_boundary = now - timedelta(days=30)
    recent_sales_30d = sales_qs.filter(
        transaction_date__gte=recent_30d_boundary
    ).aggregate(total=Sum('total_amount'))['total'] or 0
    recent_sales_spark = _sparkline_points(
        _daily_sales_series(sales_qs, 'total_amount', days=30), width=90
    )
    recent_sales_delta = _sales_period_delta_pct(sales_qs, 'total_amount', days=30)

    total_transactions_alltime = sales_qs.count()
    total_transactions_spark = _sparkline_points(_daily_sales_series(sales_qs, days=7))
    total_transactions_delta = _sales_period_delta_pct(sales_qs, days=7)

    # order_date is a plain DateField (no tz component), so it's bucketed
    # directly rather than through _daily_sales_series/_sales_period_delta_pct.
    proc_start = today - timedelta(days=6)
    proc_buckets = {proc_start + timedelta(n): 0 for n in range(7)}
    for day, cost in procurement_qs.filter(
        order_date__gte=proc_start
    ).values_list('order_date', 'total_cost'):
        if day in proc_buckets:
            proc_buckets[day] += cost or 0
    procurement_cost_spark = _sparkline_points(
        [proc_buckets[proc_start + timedelta(n)] for n in range(7)]
    )

    total_procurement_cost = procurement_qs.aggregate(total=Sum('total_cost'))['total'] or 0
    proc_prev_start = proc_start - timedelta(days=7)
    proc_current_cost = procurement_qs.filter(
        order_date__gte=proc_start
    ).aggregate(total=Sum('total_cost'))['total'] or 0
    proc_previous_cost = procurement_qs.filter(
        order_date__gte=proc_prev_start, order_date__lt=proc_start
    ).aggregate(total=Sum('total_cost'))['total'] or 0
    procurement_cost_delta = (
        round(((proc_current_cost - proc_previous_cost) / proc_previous_cost) * 100, 1)
        if proc_previous_cost else None
    )

    # --- Restock suggestions (same engine as the Alerts Center) ---
    branch_ids = branch_ids_for_user(request.user)
    restock = low_stock_alerts(branch_ids)

    # --- Branch status row: 3-second scan of each branch's sales today ---
    branches = Branch.objects.all() if branch_ids is None else Branch.objects.filter(id__in=branch_ids)
    branch_status = _branch_status_rows(branches, sales_qs, restock, today, today_sales)

    recent_orders = sales_qs.select_related('branch', 'customer').order_by(
        '-transaction_date'
    )[:6]

    return render(request, 'reports/dashboard.html', {
        'total_sales_alltime': total_sales_alltime,
        'total_sales_spark': total_sales_spark,
        'total_sales_delta': total_sales_delta,
        'recent_sales_30d': recent_sales_30d,
        'recent_sales_spark': recent_sales_spark,
        'recent_sales_delta': recent_sales_delta,
        'total_procurement_cost': total_procurement_cost,
        'procurement_cost_spark': procurement_cost_spark,
        'procurement_cost_delta': procurement_cost_delta,
        'total_transactions_alltime': total_transactions_alltime,
        'total_transactions_spark': total_transactions_spark,
        'total_transactions_delta': total_transactions_delta,
        'pending_online': pending_online,
        'inventory_value': inventory_value,
        'total_products': Product.objects.count(),
        'low_stock_count': len(restock),
        'restock': restock[:6],
        'branch_status': branch_status,
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
            sales_qs = sales_qs.filter(transaction_date__year=year, transaction_date__month=month)
        except ValueError:
            month_filter = ''

    sales_qs = sales_qs.order_by('-transaction_date')

    total_orders = sales_qs.count()
    total_sales = sales_qs.aggregate(t=Sum('total_amount'))['t'] or 0
    avg_order_value = (total_sales / total_orders) if total_orders else 0
    online_orders = sales_qs.filter(
        transaction_type=SalesTransaction.TransactionType.ONLINE
    ).count()

    return render(request, 'reports/reports_home.html', {
        'transactions': sales_qs,
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


def _branch_status_rows(branches, sales_qs, restock, today, total_today_sales):
    """
    Per-branch sales status for the Dashboard's "at a glance" row.

    status: 'critical' (no sales recorded today), 'watch' (sales down vs
    yesterday), or 'ok'. Kept separate from stock risk — low_stock_count/
    critical_count are shown alongside but don't drive the sales pill.
    """
    yesterday = today - timedelta(days=1)
    rows = []
    for branch in branches:
        b_today_qs = sales_qs.filter(branch=branch, transaction_date__date=today)
        b_today_sales = b_today_qs.aggregate(total=Sum('total_amount'))['total'] or 0
        b_today_orders = b_today_qs.count()

        b_yesterday_sales = sales_qs.filter(
            branch=branch, transaction_date__date=yesterday
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        if b_yesterday_sales:
            delta_pct = round(((b_today_sales - b_yesterday_sales) / b_yesterday_sales) * 100, 1)
        else:
            delta_pct = None

        branch_alerts = [r for r in restock if r['branch'] and r['branch'].id == branch.id]
        critical_count = sum(1 for r in branch_alerts if r['risk'] == 'Critical')

        if b_today_orders == 0:
            status = 'critical'
        elif delta_pct is not None and delta_pct < 0:
            status = 'watch'
        else:
            status = 'ok'

        rows.append({
            'branch': branch,
            'today_sales': b_today_sales,
            'delta_pct': delta_pct,
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
    if date_from:
        try:
            records_qs = records_qs.filter(
                date_recorded__date__gte=datetime.strptime(date_from, '%Y-%m-%d').date()
            )
        except ValueError:
            date_from = ''
    if date_to:
        try:
            records_qs = records_qs.filter(
                date_recorded__date__lte=datetime.strptime(date_to, '%Y-%m-%d').date()
            )
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
