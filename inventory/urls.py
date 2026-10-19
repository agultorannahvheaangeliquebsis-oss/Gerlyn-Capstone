from django.urls import path

from . import stock_count_views, views

urlpatterns = [
    # Product Master List
    path('products/', views.product_list, name='product_list'),
    path('products/add/', views.product_add, name='product_add'),
    path('products/edit/<int:pk>/', views.product_edit, name='product_edit'),
    path('products/delete/<int:pk>/', views.product_delete, name='product_delete'),
    path('products/export/', views.product_export_xlsx, name='product_export_xlsx'),
    path('products/import/', views.product_import_upload, name='product_import_upload'),
    path('products/import/map/', views.product_import_map, name='product_import_map'),
    path('products/import/preview/', views.product_import_preview, name='product_import_preview'),
    path('products/import/cancel/', views.product_import_cancel, name='product_import_cancel'),
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
    path('stock/opening/import/', views.opening_stock_import, name='opening_stock_import'),
    path('stock/opening/import/confirm/', views.opening_stock_confirm, name='opening_stock_confirm'),
    path('stock/opening/import/cancel/', views.opening_stock_cancel, name='opening_stock_cancel'),
    path('stock/opening/import/template/', views.opening_stock_template, name='opening_stock_template'),
    path('stock/opening/history/', views.opening_stock_import_history, name='opening_stock_import_history'),
    path('stock/opening/history/<int:pk>/', views.opening_stock_import_detail, name='opening_stock_import_detail'),
    path('stock/opening/history/<int:pk>/reverse/', views.opening_stock_import_reverse, name='opening_stock_import_reverse'),
    path('warehouse/', views.warehouse_stock_list, name='warehouse_stock_list'),
    path('warehouse/<int:pk>/edit/', views.warehouse_stock_edit, name='warehouse_stock_edit'),
    path('warehouse/request/', views.warehouse_stock_request, name='warehouse_stock_request'),
    path('warehouse/request/bulk/', views.warehouse_stock_bulk_request, name='warehouse_stock_bulk_request'),

    # Stock counts (cycle counts) — see inventory/stock_count.py
    path('counts/', stock_count_views.stock_count_list, name='stock_count_list'),
    path('counts/new/', stock_count_views.stock_count_start, name='stock_count_start'),
    path('counts/<int:pk>/', stock_count_views.stock_count_detail, name='stock_count_detail'),
    path('counts/<int:pk>/count/', stock_count_views.stock_count_count, name='stock_count_count'),
    path('counts/<int:pk>/scan/', stock_count_views.stock_count_scan, name='stock_count_scan'),
    path('counts/<int:pk>/review/', stock_count_views.stock_count_review, name='stock_count_review'),
    path('counts/<int:pk>/reject/', stock_count_views.stock_count_reject, name='stock_count_reject'),
    path('counts/<int:pk>/recount/', stock_count_views.stock_count_recount, name='stock_count_recount'),
    path('counts/<int:pk>/cancel/', stock_count_views.stock_count_cancel, name='stock_count_cancel'),

    # Alerts Center
    path('alerts/', views.alerts_center, name='alerts_center'),
    path('alerts/write-off/<int:pk>/', views.expiry_write_off, name='expiry_write_off'),

    # Withdrawal slips (stock transfers)
    path('withdrawals/', views.transfer_list, name='transfer_list'),
    path('withdrawals/new/', views.transfer_create, name='transfer_create'),
    path('withdrawals/<int:pk>/', views.transfer_detail, name='transfer_detail'),
    path('withdrawals/<int:pk>/approve/', views.transfer_approve, name='transfer_approve'),
    path('withdrawals/<int:pk>/release/', views.transfer_complete, name='transfer_complete'),
    path('withdrawals/<int:pk>/scan/', views.transfer_scan, name='transfer_scan'),
    path('withdrawals/<int:pk>/pick/', views.transfer_pick, name='transfer_pick'),
    path('withdrawals/<int:pk>/confirm/', views.transfer_confirm_receipt, name='transfer_confirm_receipt'),
    path('withdrawals/<int:pk>/flag/', views.transfer_flag_discrepancy, name='transfer_flag_discrepancy'),
]
