from django.urls import path

from . import views

urlpatterns = [
    path('', views.audit_log_list, name='audit_log_list'),
    path('action/<str:group_key>/', views.audit_action_detail, name='audit_action_detail'),
]
