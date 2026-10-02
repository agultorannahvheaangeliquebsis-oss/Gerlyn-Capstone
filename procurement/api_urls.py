from django.urls import path
from .views import (
    ProcurementListCreateAPIView,
    ProcurementDetailAPIView,
    ProcurementStatusUpdateAPIView,
    ProcurementReceiveAPIView,
    SupplierListAPIView,
    SupplierProductListAPIView,
    ProcurementCommunicateAPIView,
)

urlpatterns = [
    path('', ProcurementListCreateAPIView.as_view(), name='api-procurement-list'),
    path('suppliers/', SupplierListAPIView.as_view(), name='api-procurement-suppliers'),
    path(
        'suppliers/<int:pk>/products/',
        SupplierProductListAPIView.as_view(),
        name='api-procurement-supplier-products',
    ),
    path('<int:pk>/', ProcurementDetailAPIView.as_view(), name='api-procurement-detail'),
    path('<int:pk>/update_status/', ProcurementStatusUpdateAPIView.as_view(), name='api-procurement-status'),
    path('<int:pk>/receive/', ProcurementReceiveAPIView.as_view(), name='api-procurement-receive'),
    path('<int:pk>/communicate/', ProcurementCommunicateAPIView.as_view(), name='api-procurement-communicate'),
]
