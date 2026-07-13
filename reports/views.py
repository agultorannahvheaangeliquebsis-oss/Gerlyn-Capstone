from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, F
from django.utils import timezone
from datetime import timedelta

from sales.models import SalesTransaction, SalesItem
from inventory.models import Inventory, Product
from procurement.models import Procurement
from branches.models import Branch
from accounts.models import User


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
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        messages.error(request, 'You do not have permission to view reports.')
        return render(request, 'reports/no_access.html')

    sales_qs = SalesTransaction.objects.all()
    procurement_qs = Procurement.objects.all()
    inventory_qs = Inventory.objects.select_related('product', 'branch').all()

    # Branch Managers only see their own branch's data
    user_branch = get_user_branch(request.user)
    if user_branch:
        sales_qs = sales_qs.filter(branch=user_branch)
        procurement_qs = procurement_qs.filter(branch=user_branch)
        inventory_qs = inventory_qs.filter(branch=user_branch)

    # --- Sales Summary ---
    total_sales = sales_qs.aggregate(total=Sum('total_amount'))['total'] or 0
    total_transactions = sales_qs.count()

    last_30_days = timezone.now() - timedelta(days=30)
    recent_sales = sales_qs.filter(transaction_date__gte=last_30_days).aggregate(
        total=Sum('total_amount')
    )['total'] or 0

    # --- Sales by Branch ---
    sales_by_branch = sales_qs.values('branch__branch_name').annotate(
        total=Sum('total_amount'), count=Count('id')
    ).order_by('-total')

    # --- Top Selling Products ---
    top_products = SalesItem.objects.filter(transaction__in=sales_qs).values(
        'product__product_name'
    ).annotate(
        total_qty=Sum('quantity_sold'),
        total_revenue=Sum(F('quantity_sold') * F('unit_price'))
    ).order_by('-total_qty')[:5]

    # --- Low Stock Alerts ---
    low_stock = inventory_qs.filter(restock=True)
    low_stock_count = low_stock.count()

    # --- Procurement Summary ---
    total_procurement_cost = procurement_qs.aggregate(total=Sum('total_cost'))['total'] or 0
    pending_orders = procurement_qs.filter(status='Ordered').count()

    # --- Total Products ---
    total_products = Product.objects.count()

    context = {
        'total_products': total_products,
        'total_sales': total_sales,
        'total_transactions': total_transactions,
        'recent_sales': recent_sales,
        'sales_by_branch': sales_by_branch,
        'top_products': top_products,
        'low_stock': low_stock,
        'low_stock_count': low_stock_count,
        'total_procurement_cost': total_procurement_cost,
        'pending_orders': pending_orders,
        'is_owner': request.user.role == User.Role.OWNER,
    }
    return render(request, 'accounts/owner_dashboard.html', context)


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
    return render(request, 'reports/procurement_report.html', {'procurements': procurement_qs})