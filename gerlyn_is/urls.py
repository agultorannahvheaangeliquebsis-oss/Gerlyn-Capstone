from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve
from billing.views import payment_proof_download

from core.views import (
    about_view, desktop_download_view, download_view, healthz, hero_draft_apply,
    hero_draft_discard, hero_slide_delete, hero_slide_save,
    internal_settings_view, service_worker_view, store_settings_update,
    notification_center_view, notification_mark_all_read,
    notification_mark_read, notification_resolve,
)

urlpatterns = [
    # Railway's deploy health check (railway.json healthcheckPath) — see
    # core.views.healthz for why it deliberately does no DB query.
    path('healthz/', healthz, name='healthz'),
    # Served at the site root (not /static/js/) so the service worker's
    # default scope covers the whole app instead of just /static/js/.
    path('sw.js', service_worker_view, name='service_worker'),
    # Offline queue: connectivity/session probe + one-change-at-a-time sync.
    path('offline/', include('offline_sync.urls')),
    path('about/', about_view, name='about'),
    path('download/', download_view, name='download'),
    path('download/desktop/', desktop_download_view, name='desktop_download'),
    path('internal/settings/', internal_settings_view, name='internal_settings'),
    path('internal/settings/store/', store_settings_update, name='store_settings_update'),
    path('internal/settings/hero/save/', hero_slide_save, name='hero_slide_save'),
    path('internal/settings/hero/delete/', hero_slide_delete, name='hero_slide_delete'),
    path('internal/settings/hero/apply/', hero_draft_apply, name='hero_draft_apply'),
    path('internal/settings/hero/discard/', hero_draft_discard, name='hero_draft_discard'),
    path('internal/notifications/', notification_center_view, name='notification_center'),
    path('internal/notifications/<int:pk>/read/', notification_mark_read, name='notification_mark_read'),
    path('internal/notifications/<int:pk>/resolve/', notification_resolve, name='notification_resolve'),
    path('internal/notifications/read-all/', notification_mark_all_read, name='notification_mark_all_read'),
    path('', include('accounts.urls', namespace='accounts')),
    path('inventory/', include('inventory.urls')),
    path('damage-reports/', include('damage_reports.urls')),
    path('expenses/', include('expenses.urls')),
    path('branches/', include('branches.urls')),
    path('sales/', include('sales.urls')),
    path('procurement/', include('procurement.urls')),
    path('billing/', include('billing.urls')),
    path('reports/', include('reports.urls')),
    path('monitoring/audit-log/', include('audit.urls')),
    path('monitoring/backups/', include('backups.urls')),
    path('shop/', include('orders.urls')),
    path('api/', include('inventory.api_urls')),
    path('api/auth/', include('accounts.api_urls')),
    path('api/procurement/', include('procurement.api_urls')),
    path('api/sales/', include('sales.api_urls')),
    path('api/billing/', include('billing.api_urls')),
    path('api/branches/', include('branches.api_urls')),
    path('api/reports/', include('reports.api_urls')),
]

# Media (user uploads: GCash payment proofs).
#
# WhiteNoise serves STATIC only, and django.conf.urls.static.static() is a no-op
# once DEBUG=False — so without this the uploaded proof images 404 in production
# and the billing audit trail loses its evidence.
#
# Serving uploads through Django is not ideal at scale, but this is a
# single-store system with light traffic, and it keeps the deployment to one
# service with no object-storage dependency. If upload volume ever grows, move
# MEDIA_ROOT to S3/Cloudinary.
#
# NOTE: hosts like Railway have an EPHEMERAL filesystem — uploads are wiped on
# every redeploy unless MEDIA_ROOT is on a mounted volume. See ROADMAP.md.
#
# F-06: payment_proofs/ is deliberately EXCLUDED here. Those files are the
# billing audit trail's evidence (customer name, reference number, amount) and
# django.views.static.serve does no authentication — anyone with the path could
# read them. They are now reachable only through authorization-checked billing
# views, which apply the same branch/customer scoping as the billing pages.
# Only storefront assets are public. In particular, an uploaded avatar must
# never be retrievable by copying its old /media/avatars/... path.
urlpatterns += [
    path(
        'billing/<int:pk>/payment-proof/',
        payment_proof_download,
        name='payment_proof_download',
    ),
    re_path(
        r'^media/(?P<path>(?:products|categories|store)/[^?]+)$',
        serve, {'document_root': settings.MEDIA_ROOT},
    ),
]

# Django admin: only registered when settings.ADMIN_ENABLED (always in local
# DEBUG, off on hosted deployments unless DJANGO_ADMIN_ENABLED=True), at
# settings.ADMIN_URL. When off the path is simply unrouted and returns the
# normal 404. See core.admin_site for the superuser-only gate.
if settings.ADMIN_ENABLED:
    urlpatterns.insert(0, path(settings.ADMIN_URL, admin.site.urls))

# Local-only SQL/query panel — settings.py only installs the app and its
# middleware when DEBUG is on, so this import is safe even though
# debug_toolbar isn't a production dependency in spirit (it stays out of
# urlpatterns entirely when DEBUG=False).
if settings.DEBUG:
    import debug_toolbar
    urlpatterns += [path('__debug__/', include(debug_toolbar.urls))]
