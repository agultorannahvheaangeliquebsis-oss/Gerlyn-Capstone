from django.urls import path

from . import views

urlpatterns = [
    path('', views.damage_report_list, name='damage_report_list'),
    path('new/', views.damage_report_create, name='damage_report_create'),
    path('<int:pk>/', views.damage_report_detail, name='damage_report_detail'),
    path('<int:pk>/review/', views.damage_report_review, name='damage_report_review'),
    path('<int:pk>/approve/', views.damage_report_approve, name='damage_report_approve'),
    path('<int:pk>/reject/', views.damage_report_reject, name='damage_report_reject'),
    path('<int:pk>/resolve/', views.damage_report_resolve, name='damage_report_resolve'),
    path('<int:pk>/photos/<int:photo_pk>/', views.damage_report_photo, name='damage_report_photo'),
]
