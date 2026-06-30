from django.urls import path
from . import views

urlpatterns = [
    path('', views.procurement_list, name='procurement_list'),
    path('add/', views.procurement_add, name='procurement_add'),
    path('<int:pk>/', views.procurement_detail, name='procurement_detail'),
    path('<int:pk>/update-status/', views.procurement_update_status, name='procurement_update_status'),
]