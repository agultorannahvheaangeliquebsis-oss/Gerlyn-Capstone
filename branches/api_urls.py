from rest_framework.routers import DefaultRouter
from .api_views import BranchViewSet

router = DefaultRouter()
router.register(r'', BranchViewSet, basename='api-branch')

urlpatterns = router.urls
