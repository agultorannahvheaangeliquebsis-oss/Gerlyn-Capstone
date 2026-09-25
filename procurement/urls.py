from django.urls import path

from . import views

urlpatterns = [
    # Purchase orders
    path('', views.procurement_list, name='procurement_list'),
    path('add/', views.procurement_add, name='procurement_add'),
    path('<int:pk>/', views.procurement_detail, name='procurement_detail'),
    path('<int:pk>/print/', views.procurement_print, name='procurement_print'),
    path('<int:pk>/pdf/', views.procurement_pdf, name='procurement_pdf'),
    path('<int:pk>/update-status/', views.procurement_update_status,
         name='procurement_update_status'),
    path('<int:pk>/approve/', views.procurement_approve,
         name='procurement_approve'),
    path('<int:pk>/funding/', views.procurement_set_funding,
         name='procurement_set_funding'),
    path('<int:pk>/reschedule/', views.procurement_reschedule,
         name='procurement_reschedule'),

    # Negotiation — price and delivery date, agreed between the buyer and
    # the supplier on an Approved PO. See procurement/views.py's own
    # NEGOTIATION section comment for the full state shape.
    path('<int:pk>/respond/', views.procurement_owner_respond,
         name='procurement_owner_respond'),

    # Deliveries (receive a PO into stock)
    path('deliveries/', views.delivery_list, name='delivery_list'),
    path('deliveries/<int:pk>/receive/', views.delivery_receive, name='delivery_receive'),
    path('deliveries/<int:pk>/scan/', views.delivery_scan, name='delivery_scan'),
    path('deliveries/<int:pk>/add-unexpected/', views.delivery_add_unexpected,
         name='delivery_add_unexpected'),

    # Supplier's own portal (role = SUPPLIER)
    path('portal/', views.supplier_portal, name='supplier_portal'),
    path('portal/<int:pk>/respond/', views.procurement_supplier_respond,
         name='procurement_supplier_respond'),
    path('portal/<int:pk>/confirm/', views.procurement_supplier_confirm,
         name='procurement_supplier_confirm'),
    path('portal/notifications/', views.supplier_notifications,
         name='supplier_notifications'),
    path('portal/notifications/<int:pk>/read/', views.supplier_notification_mark_read,
         name='supplier_notification_mark_read'),
    path('portal/notifications/read-all/', views.supplier_notification_mark_all_read,
         name='supplier_notification_mark_all_read'),
    path('portal/products/<int:link_pk>/cost/', views.supplier_set_product_cost,
         name='supplier_set_product_cost'),
    path('portal/products/add/', views.supplier_product_self_add,
         name='supplier_product_self_add'),
    path('portal/products/<int:link_pk>/remove/', views.supplier_product_self_remove,
         name='supplier_product_self_remove'),
    path('portal/products/request/', views.supplier_product_request_add,
         name='supplier_product_request_add'),
    path('portal/products/requests/<int:req_pk>/withdraw/',
         views.supplier_product_request_withdraw,
         name='supplier_product_request_withdraw'),

    # Suppliers (managed by the store)
    path('suppliers/', views.supplier_list, name='supplier_list'),
    path('suppliers/add/', views.supplier_form, name='supplier_add'),
    path('suppliers/categories/', views.supplier_category_list,
         name='supplier_category_list'),
    path('suppliers/categories/add/', views.supplier_category_save,
         name='supplier_category_add'),
    path('suppliers/categories/<int:pk>/edit/', views.supplier_category_save,
         name='supplier_category_edit'),
    path('suppliers/categories/<int:pk>/delete/', views.supplier_category_delete,
         name='supplier_category_delete'),
    path('suppliers/<int:pk>/', views.supplier_detail, name='supplier_detail'),
    path('suppliers/<int:pk>/edit/', views.supplier_form, name='supplier_edit'),
    path('suppliers/<int:pk>/delete/', views.supplier_delete, name='supplier_delete'),

    # Per-supplier product catalog (the sub-view under a supplier)
    path('suppliers/<int:pk>/products/add/', views.supplier_product_add,
         name='supplier_product_add'),
    path('suppliers/<int:pk>/products/<int:link_pk>/remove/',
         views.supplier_product_remove, name='supplier_product_remove'),
    path('suppliers/<int:pk>/offers/<int:link_pk>/review/',
         views.supplier_offer_review, name='supplier_offer_review'),
    path('suppliers/<int:pk>/requests/<int:req_pk>/review/',
         views.supplier_request_review, name='supplier_request_review'),
    path('suppliers/<int:pk>/products/transfer/', views.supplier_product_transfer,
         name='supplier_product_transfer'),
]
