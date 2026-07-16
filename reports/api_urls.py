from django.urls import path
from .api_views import (
    DashboardSummaryAPIView,
    SalesReportAPIView,
    InventoryReportAPIView,
    ProcurementReportAPIView,
)

urlpatterns = [
    path('dashboard-summary/', DashboardSummaryAPIView.as_view(), name='api-reports-dashboard'),
    path('sales-report/', SalesReportAPIView.as_view(), name='api-reports-sales'),
    path('inventory-report/', InventoryReportAPIView.as_view(), name='api-reports-inventory'),
    path('procurement-report/', ProcurementReportAPIView.as_view(), name='api-reports-procurement'),
]
