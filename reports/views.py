import csv

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, F
from django.http import HttpResponse
from django.utils import timezone
from datetime import timedelta

from billing.models import BillingRecord

from sales.models import SalesTransaction, SalesItem
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


@login_required(login_url='login')
def reports_dashboard(request):
    """
    The internal Dashboard. Per the redesign: ONE hero metric (today's sales)
    plus a quiet strip of secondary stats — not eight competing cards.
    Branch-scoped: managers/staff see only their own branch.
    """
    if request.user.role not in [
        User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF
    ]:
        messages.error(request, 'You do not have permission to view this page.')
        return render(request, 'reports/no_access.html')

    sales_qs = SalesTransaction.objects.all()
    procurement_qs = Procurement.objects.all()
    inventory_qs = Inventory.objects.select_related('product', 'branch').all()

    user_branch = get_user_branch(request.user)
    if user_branch:
        sales_qs = sales_qs.filter(branch=user_branch)
        procurement_qs = procurement_qs.filter(branch=user_branch)
        inventory_qs = inventory_qs.filter(branch=user_branch)

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

    # --- Retail vs wholesale ---
    # Chapter 1: management had "limited visibility into ... wholesale
    # transactions across branches". This is that visibility.
    wholesale_qs = sales_qs.filter(order_type=SalesTransaction.OrderType.WHOLESALE)
    retail_qs = sales_qs.filter(order_type=SalesTransaction.OrderType.RETAIL)
    wholesale_sales = wholesale_qs.aggregate(t=Sum('total_amount'))['t'] or 0
    retail_sales = retail_qs.aggregate(t=Sum('total_amount'))['t'] or 0
    all_sales = wholesale_sales + retail_sales
    wholesale_share = round((wholesale_sales / all_sales) * 100, 1) if all_sales else 0

    # --- Restock suggestions (same engine as the Alerts Center) ---
    branch_ids = branch_ids_for_user(request.user)
    restock = low_stock_alerts(branch_ids)

    # --- Charts ---
    # Geometry is computed here, not in the template: SVG coordinate maths in
    # Django template tags would be unreadable and untestable.
    trend = _sales_trend(sales_qs, today, days=14)
    inventory_bars = _inventory_value_bars(inventory_qs)

    # --- Breakdown tables ---
    sales_by_branch = sales_qs.values('branch__branch_name').annotate(
        total=Sum('total_amount'), count=Count('id')
    ).order_by('-total')

    top_products = SalesItem.objects.filter(transaction__in=sales_qs).values(
        'product__product_name'
    ).annotate(
        total_qty=Sum('quantity_sold'),
        total_revenue=Sum(F('quantity_sold') * F('unit_price')),
    ).order_by('-total_qty')[:5]

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
        'retail_sales': retail_sales,
        'retail_count': retail_qs.count(),
        'wholesale_sales': wholesale_sales,
        'wholesale_count': wholesale_qs.count(),
        'wholesale_share': wholesale_share,
        'low_stock_count': len(restock),
        'restock': restock[:6],
        'sales_by_branch': sales_by_branch,
        'top_products': top_products,
        'recent_orders': recent_orders,
        'total_orders': sales_qs.count(),
        'trend': trend,
        'inventory_bars': inventory_bars,
        'is_owner': request.user.role == User.Role.OWNER,
        'user_branch': user_branch,
        'today': today,
    })


# --- chart helpers ---------------------------------------------------------
# The SVG viewBox is 0 0 100 40 and scales to whatever width the card is.
CHART_W, CHART_H = 100.0, 40.0
SERIES_COLOURS = ['#1E7A4F', '#B45309', '#2E9E6B', '#56655D']


def _sales_trend(sales_qs, today, days=14):
    """
    Daily sales per branch for the last `days`, as ready-to-render polylines.

    Returns {'series': [{name, colour, points, total}], 'labels': [...],
             'max': Decimal, 'has_data': bool}
    """
    start = today - timedelta(days=days - 1)
    window = [start + timedelta(days=i) for i in range(days)]

    rows = (
        sales_qs.filter(transaction_date__date__gte=start)
        .values('branch__branch_name', 'transaction_date__date')
        .annotate(total=Sum('total_amount'))
    )

    # branch -> {date: total}
    by_branch = {}
    for r in rows:
        name = r['branch__branch_name'] or '—'
        by_branch.setdefault(name, {})[r['transaction_date__date']] = r['total'] or 0

    peak = max(
        (v for daily in by_branch.values() for v in daily.values()),
        default=0,
    )
    if not peak:
        return {'series': [], 'labels': [], 'max': 0, 'has_data': False}

    step = CHART_W / (days - 1) if days > 1 else CHART_W
    series = []
    for i, (name, daily) in enumerate(sorted(by_branch.items())):
        points = []
        for x, day in enumerate(window):
            value = float(daily.get(day, 0))
            y = CHART_H - (value / float(peak)) * CHART_H
            points.append(f'{x * step:.2f},{y:.2f}')
        series.append({
            'name': name,
            'colour': SERIES_COLOURS[i % len(SERIES_COLOURS)],
            'points': ' '.join(points),
            'total': sum(daily.values()),
        })

    return {
        'series': series,
        'labels': [window[0].strftime('%b %-d'), window[-1].strftime('%b %-d')],
        'max': peak,
        'has_data': True,
    }


def _inventory_value_bars(inventory_qs):
    """Stock value per branch, at cost, as percentage-width bars."""
    totals = {}
    for inv in inventory_qs:
        if not inv.product or not inv.branch:
            continue
        cost = inv.product.wholesale_price or inv.product.unit_price or 0
        totals[inv.branch.branch_name] = (
            totals.get(inv.branch.branch_name, 0) + inv.quantity_on_hand * cost
        )

    peak = max(totals.values(), default=0)
    return [
        {
            'name': name,
            'value': value,
            'pct': round((float(value) / float(peak)) * 100, 1) if peak else 0,
        }
        for name, value in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


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

    inventory_qs = Inventory.objects.select_related('product', 'branch').all()
    user_branch = get_user_branch(request.user)
    if user_branch:
        inventory_qs = inventory_qs.filter(branch=user_branch)

    return render(request, 'reports/inventory_report.html', {'inventory_records': inventory_qs})


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