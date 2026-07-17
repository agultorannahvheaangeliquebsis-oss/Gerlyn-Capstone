from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('accounts.urls')),
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
urlpatterns += [
    re_path(
        r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}
    ),
]