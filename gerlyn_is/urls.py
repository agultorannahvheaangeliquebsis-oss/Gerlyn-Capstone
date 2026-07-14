from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('accounts.urls')),
    path('inventory/', include('inventory.urls')),
    path('sales/', include('sales.urls')),
    path('procurement/', include('procurement.urls')),
    path('billing/', include('billing.urls')),
    path('reports/', include('reports.urls')),
    path('shop/', include('orders.urls')),
    path('api/', include('inventory.api_urls')),
    path('api/auth/', include('accounts.api_urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)