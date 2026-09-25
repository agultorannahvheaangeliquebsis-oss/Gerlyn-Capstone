from django.urls import path
from .api_views import (
    SalesTransactionListCreateAPIView, SalesTransactionDetailAPIView,
    SalesRefundListCreateAPIView, SalesRefundDecisionAPIView,
)

urlpatterns = [
    path('', SalesTransactionListCreateAPIView.as_view(), name='api-sales-list'),
    path('<int:pk>/', SalesTransactionDetailAPIView.as_view(), name='api-sales-detail'),
    # P14 — refund workflow. GET lists an order's refunds (with their event
    # log); POST raises a request. Decisions are Owner/Branch Manager only.
    path('<int:pk>/refunds/', SalesRefundListCreateAPIView.as_view(), name='api-sales-refunds'),
    path('refunds/<int:pk>/approve/',
         SalesRefundDecisionAPIView.as_view(approve=True), name='api-sales-refund-approve'),
    path('refunds/<int:pk>/reject/',
         SalesRefundDecisionAPIView.as_view(approve=False), name='api-sales-refund-reject'),
]
