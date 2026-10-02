from django.urls import path
from . import views
from . import geo_views

app_name = 'accounts'

urlpatterns = [
    path('', views.home_view, name='home'),
    path('login/', views.login_view, name='login'),
    path('login/verify/', views.login_verify_view, name='login_verify'),
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
    path('manage/users/<int:pk>/hard-delete/', views.user_hard_delete, name='user_hard_delete'),
    path('manage/users/<int:pk>/cancel-invite/', views.user_cancel_invite, name='user_cancel_invite'),
    path('manage/users/<int:pk>/resend-invite/', views.user_resend_invite, name='user_resend_invite'),
    path('manage/customers/', views.customer_list, name='customer_list'),

    # --- New: Profile ---
    path('profile/', views.profile_view, name='profile'),
    path('profile/update/', views.update_profile, name='update_profile'),
    path('profile/update-avatar/', views.update_avatar, name='update_avatar'),

    # --- New: Email verification ---
    path('verify-email/pending/', views.verify_pending, name='verify_pending'),
    path('verify-email/resend/', views.resend_verification, name='resend_verification'),
    path('verify-email/<uuid:token>/', views.verify_email, name='verify_email'),
    path('invite/<uuid:token>/', views.staff_invite_complete, name='staff_invite_complete'),
    path('verify-email-change/<uuid:token>/', views.verify_email_change, name='verify_email_change'),

    # --- New: Notifications ---
    path('notifications/', views.notifications_view, name='notifications'),
    path('notifications/<int:pk>/read/', views.mark_notification_read, name='mark_notification_read'),
    path('notifications/read-all/', views.mark_all_notifications_read, name='mark_all_notifications_read'),

    # --- New: Addresses ---
    path('profile/addresses/', views.addresses_view, name='addresses'),
    path('profile/addresses/add/', views.address_create, name='address_create'),
    path('profile/addresses/<int:pk>/edit/', views.address_edit, name='address_edit'),
    path('profile/addresses/<int:pk>/delete/', views.address_delete, name='address_delete'),
    path('profile/addresses/<int:pk>/set-default/', views.address_set_default, name='address_set_default'),

    # --- Address picker data: cascading Region/Province/City/Barangay
    #     dropdowns, served from a bundled PSGC dataset (no external calls) ---
    path('geo/regions/', geo_views.geo_regions, name='geo_regions'),
    path('geo/provinces/', geo_views.geo_provinces, name='geo_provinces'),
    path('geo/cities/', geo_views.geo_cities, name='geo_cities'),
    path('geo/barangays/', geo_views.geo_barangays, name='geo_barangays'),
    path('geo/search/', geo_views.geo_search, name='geo_search'),
    path('geo/reverse/', geo_views.geo_reverse, name='geo_reverse'),
    path('geo/tiles/<int:zoom>/<int:x>/<int:y>/', geo_views.geo_tile, name='geo_tile'),

    # --- New: Notification Preferences ---
    path('profile/notification-preferences/', views.notification_preferences_view, name='notification_preferences'),
    path('profile/notification-preferences/update/', views.notification_preferences_update, name='notification_preferences_update'),

    # --- New: Settings hub — Sessions / Privacy & Data / Language / Help / About ---
    path('profile/sessions/', views.sessions_view, name='sessions'),
    path('profile/sessions/<int:pk>/revoke/', views.session_revoke, name='session_revoke'),
    path('profile/sessions/logout-others/', views.session_logout_all_others, name='session_logout_all_others'),
    path('profile/privacy/', views.privacy_view, name='privacy'),
    path('profile/privacy/deactivate/', views.deactivate_account_confirm, name='deactivate_account_confirm'),
    path('profile/privacy/deactivate/request-email/', views.deactivate_account_request, name='deactivate_account_request'),
    path('account-deactivation/confirm/<uuid:token>/', views.confirm_account_deactivation, name='confirm_account_deactivation'),
    path('profile/language/', views.language_view, name='language'),
    path('help-center/', views.help_center_view, name='help_center'),
    path('about/', views.about_view, name='about'),
    path('terms-privacy/', views.terms_privacy_view, name='terms_privacy'),
]

