from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve

from core.views import (
    about_view, db_latency_check, download_view, internal_settings_view,
    service_worker_view,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    # Served at the site root (not /static/js/) so the service worker's
    # default scope covers the whole app instead of just /static/js/.
    path('sw.js', service_worker_view, name='service_worker'),
    # TEMPORARY — Railway slow-page-load investigation. Owner-only. Remove
    # once resolved (see core/views.py:db_latency_check docstring).
    path('__diag/db/', db_latency_check, name='db_latency_check'),
    path('about/', about_view, name='about'),
    path('download/', download_view, name='download'),
    path('internal/settings/', internal_settings_view, name='internal_settings'),
    path('', include('accounts.urls', namespace='accounts')),
    path('inventory/', include('inventory.urls')),
    path('branches/', include('branches.urls')),
    path('sales/', include('sales.urls')),
    path('procurement/', include('procurement.urls')),
    path('billing/', include('billing.urls')),
    path('reports/', include('reports.urls')),
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
# read them. They are now reachable only through billing.views.billing_proof,
# which applies the same branch/customer scoping as the billing views. Everything
# else under MEDIA_ROOT (product images, avatars) stays public as before.
urlpatterns += [
    re_path(
        r'^media/(?!payment_proofs/)(?P<path>.*)$',
        serve, {'document_root': settings.MEDIA_ROOT},
    ),
]