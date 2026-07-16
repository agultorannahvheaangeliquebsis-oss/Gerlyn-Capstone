from django.urls import path

from . import views

urlpatterns = [
    # Purchase orders
    path('', views.procurement_list, name='procurement_list'),
    path('add/', views.procurement_add, name='procurement_add'),
    path('<int:pk>/', views.procurement_detail, name='procurement_detail'),
    path('<int:pk>/update-status/', views.procurement_update_status,
         name='procurement_update_status'),

    # Deliveries (receive a PO into stock)
    path('deliveries/', views.delivery_list, name='delivery_list'),
    path('deliveries/<int:pk>/receive/', views.delivery_receive, name='delivery_receive'),
    path('deliveries/history/', views.receiving_history, name='receiving_history'),

    # Suppliers
    path('suppliers/', views.supplier_list, name='supplier_list'),
    path('suppliers/add/', views.supplier_form, name='supplier_add'),
    path('suppliers/<int:pk>/edit/', views.supplier_form, name='supplier_edit'),
    path('suppliers/<int:pk>/delete/', views.supplier_delete, name='supplier_delete'),
]
