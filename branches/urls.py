from django.urls import path

from . import views

urlpatterns = [
    path('', views.branch_list, name='branch_list'),
    path('add/', views.branch_form, name='branch_add'),
    path('<int:pk>/edit/', views.branch_form, name='branch_edit'),
    path('<int:pk>/delete/', views.branch_delete, name='branch_delete'),
]
