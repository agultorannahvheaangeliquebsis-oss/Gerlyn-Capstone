from django.urls import path

from . import views

urlpatterns = [
    path('', views.backup_status, name='backup_status'),
    path('run-now/', views.backup_run_now, name='backup_run_now'),
    path('restore-test/', views.backup_record_restore_test, name='backup_record_restore_test'),
    path('trigger/', views.backup_trigger, name='backup_trigger'),
]
