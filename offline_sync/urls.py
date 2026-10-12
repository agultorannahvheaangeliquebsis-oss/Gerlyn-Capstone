from django.urls import path

from . import views

# No app_name: reverse('offline_ping') / reverse('offline_sync_apply'), like
# the other top-level utility routes (healthz, sw.js). The names are distinctive
# on purpose -- accounts.middleware's module-permission lookup is keyed by
# bare URL name.
urlpatterns = [
    path('ping/', views.ping, name='offline_ping'),
    path('sync/', views.sync, name='offline_sync_apply'),
]
