from django.urls import path

from . import export_views, profitability_views, views

urlpatterns = [
    path('', views.reports_dashboard, name='reports_dashboard'),
    path('sales/', views.sales_report, name='sales_report'),
    path('inventory/', views.inventory_report, name='inventory_report'),
    path('procurement/', views.procurement_report, name='procurement_report'),
    path('billing/', views.billing_report, name='billing_report'),
    path('profitability/', profitability_views.profitability_report, name='profitability_report'),
    # CSV export: kind = sales | inventory | procurement | billing
    path('export/<str:kind>.csv', views.export_csv, name='export_csv'),
    # PDF / Excel: the same kinds (sales, billing, inventory, procurement and
    # profitability), each opening with a Summary. See
    # reports/exports.py.
    path('export/<str:kind>.pdf', export_views.export_pdf, name='export_pdf'),
    path('export/<str:kind>.xlsx', export_views.export_xlsx, name='export_xlsx'),
]
