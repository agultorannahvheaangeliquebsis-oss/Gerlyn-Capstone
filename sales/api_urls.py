from django.urls import path
from .api_views import (
    SalesPwdRequestReviewAPIView, SalesTransactionDetailAPIView,
    SalesTransactionListCreateAPIView,
)

urlpatterns = [
    path('', SalesTransactionListCreateAPIView.as_view(), name='api-sales-list'),
    path('<int:pk>/', SalesTransactionDetailAPIView.as_view(), name='api-sales-detail'),
    path('<int:pk>/pwd-request/', SalesPwdRequestReviewAPIView.as_view(),
         name='api-sales-pwd-request'),
]
