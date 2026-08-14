from django.urls import path

from . import views

urlpatterns = [
    # Product Master List
    path('products/', views.product_list, name='product_list'),
    path('products/add/', views.product_add, name='product_add'),
    path('products/edit/<int:pk>/', views.product_edit, name='product_edit'),
    path('products/delete/<int:pk>/', views.product_delete, name='product_delete'),
    path('barcode-lookup/', views.barcode_lookup, name='barcode_lookup'),

    # Categories
    path('categories/', views.category_list, name='category_list'),
    path('categories/add/', views.category_form, name='category_add'),
    path('categories/<int:pk>/edit/', views.category_form, name='category_edit'),
    path('categories/<int:pk>/delete/', views.category_delete, name='category_delete'),

    # Stock
    path('stock/', views.inventory_list, name='inventory_list'),
    path('stock/adjust/', views.stock_adjustment_add, name='stock_adjustment_add'),
    path('stock/<int:pk>/edit/', views.inventory_edit, name='inventory_edit'),
    path('warehouse/', views.warehouse_stock_list, name='warehouse_stock_list'),
    path('warehouse/<int:pk>/edit/', views.warehouse_stock_edit, name='warehouse_stock_edit'),
    path('warehouse/request/', views.warehouse_stock_request, name='warehouse_stock_request'),

    # Alerts Center
    path('alerts/', views.alerts_center, name='alerts_center'),

    # Withdrawal slips (stock transfers)
    path('withdrawals/', views.transfer_list, name='transfer_list'),
    path('withdrawals/new/', views.transfer_create, name='transfer_create'),
    path('withdrawals/<int:pk>/', views.transfer_detail, name='transfer_detail'),
    path('withdrawals/<int:pk>/approve/', views.transfer_approve, name='transfer_approve'),
    path('withdrawals/<int:pk>/release/', views.transfer_complete, name='transfer_complete'),
    path('withdrawals/<int:pk>/confirm/', views.transfer_confirm_receipt, name='transfer_confirm_receipt'),
    path('withdrawals/<int:pk>/flag/', views.transfer_flag_discrepancy, name='transfer_flag_discrepancy'),
]
