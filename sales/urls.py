from django.urls import path

from . import views

urlpatterns = [
    path('', views.transaction_list, name='transaction_list'),
    path('add/', views.transaction_add, name='transaction_add'),
    path('<int:pk>/', views.transaction_detail, name='transaction_detail'),
    path('<int:pk>/receipt/', views.transaction_receipt, name='transaction_receipt'),
    # Order lifecycle: Pending -> Approved -> Preparing -> Completed / Cancelled
    path('<int:pk>/status/<str:new_status>/', views.transaction_set_status,
         name='transaction_set_status'),
    # Staff apply the statutory PWD/Senior discount after the fact, against
    # an ID they have seen in person. Walk-in or Pickup only (F-04) — a
    # Delivery order is refused server-side.
    path('<int:pk>/discount/', views.transaction_apply_discount,
         name='transaction_apply_discount'),
]
