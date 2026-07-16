from django.urls import path
from .views import (
    ProcurementListCreateAPIView,
    ProcurementDetailAPIView,
    ProcurementStatusUpdateAPIView,
)

urlpatterns = [
    path('', ProcurementListCreateAPIView.as_view(), name='api-procurement-list'),
    path('<int:pk>/', ProcurementDetailAPIView.as_view(), name='api-procurement-detail'),
    path('<int:pk>/update_status/', ProcurementStatusUpdateAPIView.as_view(), name='api-procurement-status'),
]
