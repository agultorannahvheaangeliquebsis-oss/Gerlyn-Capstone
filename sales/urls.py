from django.urls import path

from . import views

urlpatterns = [
    path('', views.transaction_list, name='transaction_list'),
    path('add/', views.transaction_add, name='transaction_add'),
    path('<int:pk>/', views.transaction_detail, name='transaction_detail'),
    # Order lifecycle: Pending -> Approved -> Preparing -> Completed / Cancelled
    path('<int:pk>/status/<str:new_status>/', views.transaction_set_status,
         name='transaction_set_status'),
]
