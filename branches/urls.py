from django.urls import path

from . import service_area_views, views

urlpatterns = [
    path('', views.branch_list, name='branch_list'),
    path('add/', views.branch_form, name='branch_add'),
    path('<int:pk>/edit/', views.branch_form, name='branch_edit'),
    path('<int:pk>/delete/', views.branch_delete, name='branch_delete'),
    path('<int:pk>/assign-manager/', views.branch_assign_manager, name='branch_assign_manager'),

    # Delivery Areas (Owner only): where each branch delivers.
    path('service-areas/', service_area_views.service_area_list, name='service_area_list'),
    path('service-areas/add/', service_area_views.service_area_add, name='service_area_add'),
    path('service-areas/<int:pk>/edit/', service_area_views.service_area_edit,
         name='service_area_edit'),
    path('service-areas/<int:pk>/toggle/', service_area_views.service_area_toggle,
         name='service_area_toggle'),
]
