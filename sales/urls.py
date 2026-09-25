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
    # Request a refund of some or all items of a Completed, Paid order, within
    # SalesTransaction.REFUND_WINDOW of completion. Anyone with the branch may
    # ask; an Owner/Branch Manager's own request is approved as it is sent.
    path('<int:pk>/refund/', views.transaction_refund, name='transaction_refund'),
    # Owner/Branch Manager only — decide a Pending refund.
    path('refunds/<int:refund_pk>/approve/', views.transaction_refund_approve,
         name='transaction_refund_approve'),
    path('refunds/<int:refund_pk>/reject/', views.transaction_refund_reject,
         name='transaction_refund_reject'),
]
