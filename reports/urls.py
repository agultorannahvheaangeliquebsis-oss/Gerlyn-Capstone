from django.urls import path

from . import views

urlpatterns = [
    path('', views.reports_dashboard, name='reports_dashboard'),
    path('sales/', views.sales_report, name='sales_report'),
    path('inventory/', views.inventory_report, name='inventory_report'),
    path('procurement/', views.procurement_report, name='procurement_report'),
    path('billing/', views.billing_report, name='billing_report'),
    # CSV export: kind = sales | inventory | procurement | billing
    path('export/<str:kind>.csv', views.export_csv, name='export_csv'),
]
