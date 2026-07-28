from django.urls import path
from . import views
from . import geo_views

app_name = 'accounts'

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
    path('profile/change-password/', views.change_password, name='change_password'),
    path('profile/two-factor/toggle/', views.toggle_two_factor, name='toggle_two_factor'),
    path('profile/change-password/request-email/', views.request_password_change_email, name='request_password_change_email'),
    path('password-change/confirm/<uuid:token>/', views.confirm_password_change, name='confirm_password_change'),

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

    # --- New: Notifications ---
    path('notifications/', views.notifications_view, name='notifications'),
    path('notifications/<int:pk>/read/', views.mark_notification_read, name='mark_notification_read'),

    # --- New: Addresses ---
    path('profile/addresses/', views.addresses_view, name='addresses'),
    path('profile/addresses/add/', views.address_create, name='address_create'),
    path('profile/addresses/<int:pk>/edit/', views.address_edit, name='address_edit'),
    path('profile/addresses/<int:pk>/delete/', views.address_delete, name='address_delete'),
    path('profile/addresses/<int:pk>/set-default/', views.address_set_default, name='address_set_default'),

    # --- New: Address picker data (cascading dropdowns + geocoding proxy) ---
    path('geo/regions/', geo_views.geo_regions, name='geo_regions'),
    path('geo/provinces/', geo_views.geo_provinces, name='geo_provinces'),
    path('geo/cities/', geo_views.geo_cities, name='geo_cities'),
    path('geo/barangays/', geo_views.geo_barangays, name='geo_barangays'),
    path('geo/search/', geo_views.geo_search, name='geo_search'),
    path('geo/reverse/', geo_views.geo_reverse, name='geo_reverse'),

    # --- New: Notification Preferences ---
    path('profile/notification-preferences/', views.notification_preferences_view, name='notification_preferences'),
    path('profile/notification-preferences/update/', views.notification_preferences_update, name='notification_preferences_update'),

    # --- New: Settings hub — Sessions / Privacy & Data / Language / Help / About ---
    path('profile/sessions/', views.settings_placeholder_view, {'page': 'sessions'}, name='sessions'),
    path('profile/privacy/', views.settings_placeholder_view, {'page': 'privacy'}, name='privacy'),
    path('profile/language/', views.settings_placeholder_view, {'page': 'language'}, name='language'),
    path('help-center/', views.help_center_view, name='help_center'),
    path('about/', views.about_view, name='about'),
]

