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


@login_required(login_url='login')
def operational_dashboard(request):
    """
    The Dashboard — a live operational snapshot: today's sales, per-branch
    status, restock alerts, recent orders. Historical/analytical breakdowns
    (trend, branch & product performance) live on the Reports page instead
    (see reports_dashboard) so the two pages aren't the same content twice.
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

    # --- Hero metric: today's sales ---
    today_qs = sales_qs.filter(transaction_date__date=today)
    today_sales = today_qs.aggregate(total=Sum('total_amount'))['total'] or 0
    today_orders = today_qs.count()

    yesterday_sales = sales_qs.filter(
        transaction_date__date=today - timedelta(days=1)
    ).aggregate(total=Sum('total_amount'))['total'] or 0
    if yesterday_sales:
        delta_pct = round(((today_sales - yesterday_sales) / yesterday_sales) * 100, 1)
    else:
        delta_pct = None

    # --- Quiet strip ---
    month_sales = sales_qs.filter(
        transaction_date__year=today.year, transaction_date__month=today.month
    ).aggregate(total=Sum('total_amount'))['total'] or 0

    pending_online = sales_qs.filter(
        transaction_type=SalesTransaction.TransactionType.ONLINE,
        order_status=SalesTransaction.OrderStatus.PENDING,
    ).count()
    open_pos = procurement_qs.filter(status='Ordered').count()

    # Inventory value at cost (wholesale price where known).
    inventory_value = sum(
        inv.quantity_on_hand * (inv.product.wholesale_price or inv.product.unit_price or 0)
        for inv in inventory_qs if inv.product
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
        'today_sales': today_sales,
        'today_orders': today_orders,
        'delta_pct': delta_pct,
        'month_sales': month_sales,
        'pending_online': pending_online,
        'open_pos': open_pos,
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


@login_required(login_url='login')
def reports_dashboard(request):
    """
    The Reports hub — opens straight onto the Sales Report: every transaction
    the user's role can see, filterable by branch (Owner only — a Manager/
    Staff's own branch is already the whole story) and by month, with the
    same CSV/print-to-PDF export as the other per-process reports.
    """
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
    ]:
        messages.error(request, 'You do not have permission to view this page.')
        return render(request, 'reports/no_access.html')

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

    records = BillingRecord.objects.select_related('transaction', 'transaction__branch')
    user_branch = get_user_branch(request.user)
    if user_branch:
        records = records.filter(transaction__branch=user_branch)

    records = records.order_by('-date_recorded')
    outstanding = sum(
        r.amount_due for r in records
        if r.status == BillingRecord.PaymentStatus.PENDING
    )
    collected = sum(
        r.amount_due for r in records
        if r.status == BillingRecord.PaymentStatus.PAID
    )

    return render(request, 'reports/billing_report.html', {
        'records': records,
        'outstanding': outstanding,
        'collected': collected,
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

    procurement_qs = Procurement.objects.select_related('branch', 'supplier').all()
    user_branch = get_user_branch(request.user)
    if user_branch:
        procurement_qs = procurement_qs.filter(branch=user_branch)

    procurement_qs = procurement_qs.order_by('-order_date')
    return render(request, 'reports/procurement.html', {'procurements': procurement_qs})