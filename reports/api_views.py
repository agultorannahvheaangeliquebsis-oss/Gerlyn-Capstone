# reports/api_views.py
# Mirrors reports/views.py one-for-one, returning JSON instead of a
# rendered template. No models of its own — reports are pure aggregation
# over sales/inventory/procurement, so there are no serializers either.
#
# DashboardSummaryAPIView used to keep its own copy of every figure it
# computed — summing every order_status into "sales", reading the
# potentially-stale Inventory.restock flag for low stock, and including
# Cancelled purchase orders in procurement cost — so it could (and did)
# disagree with reports_dashboard on the same underlying rows. It now reads
# reports/services.py, the same shared query layer reports/views.py reads,
# so the two can no longer drift apart. See that module's docstring for the
# Gross Sales / Refund Value / Net Sales / Pipeline Value / Procurement Cost
# definitions used below.

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from core.permissions import HasModuleAccess
from django.db.models import Sum, Count, F

from sales.models import SalesTransaction, SalesItem
from inventory.models import Inventory, Product
from procurement.models import Procurement
from accounts.models import User
from core.timebounds import local_range_bounds, range_from_request
from reports.services import (
    gross_sales_total, low_stock_count, pipeline_sales, pipeline_value_total,
    procurement_cost_total, refund_value_total, revenue_sales,
    scoped_report_querysets,
)


# Branch scoping lives in core.scoping (via reports.services.scoped_report_
# querysets) so the API and the template views can never drift apart — this
# is security-critical logic, it gets ONE definition.
from core.scoping import scope_queryset  # noqa: E402


def check_report_access(request):
    if request.user.role not in [User.Role.OWNER, User.Role.BRANCH_MANAGER]:
        return Response(
            {'detail': 'You do not have permission to view reports.'},
            status=status.HTTP_403_FORBIDDEN
        )
    return None


class DashboardSummaryAPIView(APIView):
    """GET /api/reports/dashboard-summary/ — mirrors reports_dashboard.

    Accepts the same query params reports_dashboard reads (`range`,
    `date_from`, `date_to` for the shared date-range control, plus `branch`
    for an Owner — see core.timebounds.range_from_request), so a request
    carrying the same params as a reports_dashboard page load, from the
    same user, returns the identical figures below.
    """
    permission_classes = [IsAuthenticated, HasModuleAccess]

    def get(self, request):
        denied = check_report_access(request)
        if denied:
            return denied

        branch_filter = request.GET.get('branch') or None
        sales_qs, procurement_qs, inventory_qs, _warehouse_qs, _effective_branch = (
            scoped_report_querysets(request.user, override_branch_id=branch_filter)
        )

        # Same range resolution and half-open bounds reports_dashboard uses
        # — see that view and core/timebounds for why plain date lookups on
        # transaction_date (a DateTimeField) don't work on this deployment.
        range_start, range_end, active_range = range_from_request(request)
        ranged_sales_qs = sales_qs
        _, period_end = local_range_bounds(range_end, range_end)
        ranged_sales_qs = ranged_sales_qs.filter(transaction_date__lt=period_end)
        if range_start is not None:
            period_start, _ = local_range_bounds(range_start, range_start)
            ranged_sales_qs = ranged_sales_qs.filter(transaction_date__gte=period_start)

        # order_date is a plain DateField (no tz component), so it compares
        # against the dates directly — same as procurement_report.
        ranged_procurement_qs = procurement_qs.filter(order_date__lte=range_end)
        if range_start is not None:
            ranged_procurement_qs = ranged_procurement_qs.filter(order_date__gte=range_start)

        revenue_qs = revenue_sales(ranged_sales_qs)
        total_orders = revenue_qs.count()
        gross_sales = gross_sales_total(ranged_sales_qs)
        refund_value = refund_value_total(ranged_sales_qs)
        net_sales = gross_sales - refund_value

        pipeline_qs = pipeline_sales(ranged_sales_qs)
        pipeline_value = pipeline_value_total(ranged_sales_qs)
        pipeline_count = pipeline_qs.count()

        # Completed-only, matching Gross/Net Sales above — these used to
        # read the unfiltered sales_qs, so a Pending or Cancelled order's
        # amount inflated both the per-branch split and Top Products too.
        sales_by_branch = list(revenue_qs.values('branch__branch_name').annotate(
            total=Sum('total_amount'), count=Count('id')
        ).order_by('-total'))

        top_products = list(SalesItem.objects.filter(transaction__in=revenue_qs).values(
            'product__product_name'
        ).annotate(
            total_qty=Sum('quantity_sold'),
            total_revenue=Sum(F('quantity_sold') * F('unit_price'))
        ).order_by('-total_qty')[:5])

        # Snapshots of current state, not period figures — no date-range
        # scoping applies, matching the Dashboard's own Low Stock / Pending
        # POs needs-attention counts.
        low_stock = low_stock_count(inventory_qs)
        pending_orders = procurement_qs.filter(
            status__in=Procurement.COMMITTED_OPEN_STATUSES
        ).count()
        total_procurement_cost = procurement_cost_total(ranged_procurement_qs)
        total_products = Product.objects.count()

        return Response({
            'totalProducts': total_products,
            'totalOrders': total_orders,
            # Kept for backward compatibility with existing callers of this
            # field name — now Completed orders only (see totalOrders/
            # grossSales), not every order_status.
            'totalTransactions': total_orders,
            'grossSales': gross_sales,
            'refundValue': refund_value,
            'netSales': net_sales,
            # Kept for backward compatibility with existing callers of this
            # field name — now NET of refunds, not gross Completed revenue.
            'totalSales': net_sales,
            'pipelineValue': pipeline_value,
            'pipelineCount': pipeline_count,
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
            'lowStockCount': low_stock,
            'totalProcurementCost': total_procurement_cost,
            'pendingOrders': pending_orders,
            'activeRange': active_range,
            'rangeStart': range_start,
            'rangeEnd': range_end,
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
