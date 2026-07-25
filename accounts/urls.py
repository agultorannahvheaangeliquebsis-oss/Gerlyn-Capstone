from django.urls import path
from . import views


urlpatterns = [
    path('', views.home_view, name='home'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('dashboard/manager/', views.manager_dashboard, name='manager_dashboard'),
    path('dashboard/staff/', views.staff_dashboard, name='staff_dashboard'),
    path('dashboard/supplier/', views.supplier_dashboard, name='supplier_dashboard'),
    path('dashboard/customer/', views.customer_dashboard, name='customer_dashboard'),
    path('dashboard/owner/', views.owner_dashboard, name='owner_dashboard'),
    path('register/', views.register_view, name='register'),

    # Administration — User Management (Owner only).
    # NOTE: not under /admin/ — that prefix belongs to django.contrib.admin,
    # which is registered first and would swallow these routes.
    path('manage/users/', views.user_list, name='user_list'),
    path('manage/users/add/', views.user_form, name='user_add'),
    path('manage/users/<int:pk>/edit/', views.user_form, name='user_edit'),
    path('manage/users/<int:pk>/toggle/', views.user_toggle_active, name='user_toggle_active'),

    # --- New: Profile ---
    path('profile/', views.profile_view, name='profile'),
    path('profile/update/', views.update_profile, name='update_profile'),
    path('profile/update-avatar/', views.update_avatar, name='update_avatar'),

    # --- New: Email verification ---
    path('verify-email/pending/', views.verify_pending, name='verify_pending'),
    path('verify-email/resend/', views.resend_verification, name='resend_verification'),
    path('verify-email/<uuid:token>/', views.verify_email, name='verify_email'),
    path('verify-email-change/<uuid:token>/', views.verify_email_change, name='verify_email_change'),
]

