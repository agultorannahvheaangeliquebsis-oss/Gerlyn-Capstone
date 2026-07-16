from django.urls import path
from .api_views import (
    BillingRecordListAPIView,
    BillingRecordDetailAPIView,
    BillingPaymentAPIView,
    BillingConfirmAPIView,
)

urlpatterns = [
    path('', BillingRecordListAPIView.as_view(), name='api-billing-list'),
    path('<int:pk>/', BillingRecordDetailAPIView.as_view(), name='api-billing-detail'),
    path('<int:pk>/pay/', BillingPaymentAPIView.as_view(), name='api-billing-pay'),
    path('<int:pk>/confirm/', BillingConfirmAPIView.as_view(), name='api-billing-confirm'),
]
