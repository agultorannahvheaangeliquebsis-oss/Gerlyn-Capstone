# reports/api_views.py
# Mirrors reports/views.py one-for-one, returning JSON instead of a
# rendered template. No models of its own — reports are pure aggregation
# over sales/inventory/procurement, so there are no serializers either.

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from core.permissions import HasModuleAccess
from django.db.models import Sum, Count, F
from django.utils import timezone
from datetime import timedelta

from sales.models import SalesTransaction, SalesItem
from inventory.models import Inventory, Product
from procurement.models import Procurement
from accounts.models import User


# Branch scoping lives in core.scoping so the API and the template views can
# never drift apart — this is security-critical logic, it gets ONE definition.
#
# scope_queryset(), not `if get_user_branch(user):` — branch_for_user() returns
# None both for an Owner (sees every branch) and for a Manager/Staff whose
# profile branch is NULL (on_delete=SET_NULL makes that reachable), and the
# `if` form leaks every branch to the second case. branch_ids_for_user beneath
# scope_queryset returns None only for the Owner and [] for an unassigned
# account, so it fails CLOSED.
from core.scoping import scope_queryset  # noqa: E402


def check_report_access(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        return Response(
            {'detail': 'You do not have permission to view reports.'},
            status=status.HTTP_403_FORBIDDEN
        )
    return None


class DashboardSummaryAPIView(APIView):
    """GET /api/reports/dashboard-summary/ — mirrors reports_dashboard."""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        denied = check_report_access(request)
        if denied:
            return denied

        sales_qs = SalesTransaction.objects.all()
        procurement_qs = Procurement.objects.all()
        inventory_qs = Inventory.objects.select_related('product', 'branch').all()

        sales_qs = scope_queryset(sales_qs, request.user, 'branch')
        procurement_qs = scope_queryset(procurement_qs, request.user, 'branch')
        inventory_qs = scope_queryset(inventory_qs, request.user, 'branch')

        total_sales = sales_qs.aggregate(total=Sum('total_amount'))['total'] or 0
        total_transactions = sales_qs.count()

        last_30_days = timezone.now() - timedelta(days=30)
        recent_sales = sales_qs.filter(transaction_date__gte=last_30_days).aggregate(
            total=Sum('total_amount')
        )['total'] or 0

        sales_by_branch = list(sales_qs.values('branch__branch_name').annotate(
            total=Sum('total_amount'), count=Count('id')
        ).order_by('-total'))

        top_products = list(SalesItem.objects.filter(transaction__in=sales_qs).values(
            'product__product_name'
        ).annotate(
            total_qty=Sum('quantity_sold'),
            total_revenue=Sum(F('quantity_sold') * F('unit_price'))
        ).order_by('-total_qty')[:5])

        low_stock_count = inventory_qs.filter(restock=True).count()
        total_procurement_cost = procurement_qs.aggregate(total=Sum('total_cost'))['total'] or 0
        pending_orders = procurement_qs.filter(
            status__in=Procurement.COMMITTED_OPEN_STATUSES
        ).count()
        total_products = Product.objects.count()

        return Response({
            'totalProducts': total_products,
            'totalSales': total_sales,
            'totalTransactions': total_transactions,
            'recentSales': recent_sales,
            'salesByBranch': [
                {'branch': row['branch__branch_name'], 'total': row['total'], 'count': row['count']}
                for row in sales_by_branch
            ],
            'topProducts': [
                {
                    'productName': row['product__product_name'],
                    'totalQty': row['total_qty'],
                    'totalRevenue': row['total_revenue'],
                }
                for row in top_products
            ],
            'lowStockCount': low_stock_count,
            'totalProcurementCost': total_procurement_cost,
            'pendingOrders': pending_orders,
            'isOwner': request.user.role == User.Role.OWNER,
        })


class SalesReportAPIView(APIView):
    """GET /api/reports/sales-report/ — mirrors sales_report."""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        denied = check_report_access(request)
        if denied:
            return denied

        qs = SalesTransaction.objects.select_related('branch', 'staff', 'customer').all()
        qs = scope_queryset(qs, request.user, 'branch').order_by('-transaction_date')

        from sales.serializers import SalesTransactionSerializer
        return Response(SalesTransactionSerializer(qs, many=True).data)


class InventoryReportAPIView(APIView):
    """GET /api/reports/inventory-report/ — mirrors inventory_report."""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        denied = check_report_access(request)
        if denied:
            return denied

        qs = scope_queryset(
            Inventory.objects.select_related('product', 'branch').all(),
            request.user, 'branch',
        )

        from inventory.serializers import InventorySerializer
        return Response(InventorySerializer(qs, many=True).data)


class ProcurementReportAPIView(APIView):
    """GET /api/reports/procurement-report/ — mirrors procurement_report."""
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        denied = check_report_access(request)
        if denied:
            return denied

        qs = Procurement.objects.select_related('branch', 'supplier').all()
        qs = scope_queryset(qs, request.user, 'branch').order_by('-order_date')

        from procurement.serializers import ProcurementReadSerializer
        return Response(ProcurementReadSerializer(qs, many=True).data)
