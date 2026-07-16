from django.urls import path
from .api_views import SalesTransactionListCreateAPIView, SalesTransactionDetailAPIView

urlpatterns = [
    path('', SalesTransactionListCreateAPIView.as_view(), name='api-sales-list'),
    path('<int:pk>/', SalesTransactionDetailAPIView.as_view(), name='api-sales-detail'),
]
