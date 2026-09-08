from django.urls import path

from . import views

urlpatterns = [
    path('', views.transaction_list, name='transaction_list'),
    path('add/', views.transaction_add, name='transaction_add'),
    path('<int:pk>/', views.transaction_detail, name='transaction_detail'),
    # Order lifecycle: Pending -> Approved -> Preparing -> Completed / Cancelled
    path('<int:pk>/status/<str:new_status>/', views.transaction_set_status,
         name='transaction_set_status'),

    # Customer Returns: Pending -> Approved -> Refunded, or -> Rejected
    path('returns/', views.return_list, name='return_list'),
    path('returns/new/', views.return_create, name='return_create'),
    path('returns/<int:pk>/', views.return_detail, name='return_detail'),
    path('returns/<int:pk>/approve/', views.return_approve, name='return_approve'),
    path('returns/<int:pk>/reject/', views.return_reject, name='return_reject'),
    path('returns/<int:pk>/refund/', views.return_mark_refunded, name='return_mark_refunded'),
]
