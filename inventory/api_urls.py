from rest_framework.routers import DefaultRouter
from .api_views import ProductViewSet, ProductCategoryViewSet

router = DefaultRouter()
router.register(r'products', ProductViewSet, basename='api-product')
router.register(r'categories', ProductCategoryViewSet, basename='api-category')

urlpatterns = router.urls