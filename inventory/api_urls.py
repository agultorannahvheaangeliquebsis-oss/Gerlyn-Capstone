from django.urls import path
from rest_framework.routers import DefaultRouter
from .api_views import (
    ProductViewSet,
    ProductCategoryViewSet,
    WarehouseViewSet,
    WarehouseStockViewSet,
    InventoryViewSet,
    BatchViewSet,
    StockMovementListAPIView,
    StockTransferListCreateAPIView,
    StockTransferDetailAPIView,
    StockTransferApproveAPIView,
    StockTransferReleaseAPIView,
    StockTransferAcknowledgeAPIView,
)

router = DefaultRouter()
router.register(r'products', ProductViewSet, basename='api-product')
router.register(r'categories', ProductCategoryViewSet, basename='api-category')
router.register(r'warehouses', WarehouseViewSet, basename='api-warehouse')
router.register(r'warehouse-stock', WarehouseStockViewSet, basename='api-warehouse-stock')
router.register(r'inventory', InventoryViewSet, basename='api-inventory')
router.register(r'batches', BatchViewSet, basename='api-batch')

urlpatterns = router.urls + [
    path('stock-movements/', StockMovementListAPIView.as_view(), name='api-stock-movements'),
    path('stock-transfers/', StockTransferListCreateAPIView.as_view(), name='api-stock-transfer-list'),
    path('stock-transfers/<int:pk>/', StockTransferDetailAPIView.as_view(), name='api-stock-transfer-detail'),
    path('stock-transfers/<int:pk>/approve/', StockTransferApproveAPIView.as_view(), name='api-stock-transfer-approve'),
    path('stock-transfers/<int:pk>/release/', StockTransferReleaseAPIView.as_view(), name='api-stock-transfer-release'),
    path('stock-transfers/<int:pk>/acknowledge/', StockTransferAcknowledgeAPIView.as_view(), name='api-stock-transfer-acknowledge'),
]
