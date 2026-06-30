from django.urls import path
from . import views

urlpatterns = [
    path('', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('dashboard/owner/', views.owner_dashboard, name='owner_dashboard'),
    path('dashboard/manager/', views.manager_dashboard, name='manager_dashboard'),
    path('dashboard/staff/', views.staff_dashboard, name='staff_dashboard'),
    path('dashboard/supplier/', views.supplier_dashboard, name='supplier_dashboard'),
    path('dashboard/customer/', views.customer_dashboard, name='customer_dashboard'),
]