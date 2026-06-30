from django.urls import path
from . import views

urlpatterns = [
    path('', views.billing_list, name='billing_list'),
    path('<int:pk>/', views.billing_detail, name='billing_detail'),
    path('create/<int:transaction_id>/', views.billing_create, name='billing_create'),
    path('<int:pk>/confirm/', views.billing_confirm, name='billing_confirm'),
]   