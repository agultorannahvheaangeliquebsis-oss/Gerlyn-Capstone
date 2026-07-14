from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from .models import Product, ProductCategory
from .serializers import (
    ProductSerializer,
    ProductWriteSerializer,
    ProductCategorySerializer,
)
from accounts.models import User


class ProductCategoryViewSet(viewsets.ModelViewSet):
    """
    /api/categories/
    GET    → list all categories
    POST   → create a new category (Owner/Manager only)
    """
    queryset = ProductCategory.objects.all().order_by('category_name')
    serializer_class = ProductCategorySerializer
    permission_classes = [IsAuthenticated]


class ProductViewSet(viewsets.ModelViewSet):
    """
    /api/products/
    GET    /api/products/          → list all products (with search + filter)
    GET    /api/products/<id>/     → retrieve single product
    POST   /api/products/          → create product (Owner/Manager only)
    PUT    /api/products/<id>/     → full update (Owner/Manager only)
    PATCH  /api/products/<id>/     → partial update (Owner/Manager only)
    DELETE /api/products/<id>/     → delete (Owner/Manager only)
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['product_name', 'sku_code']

    def get_queryset(self):
        queryset = Product.objects.select_related(
            'category'
        ).prefetch_related(
            'inventory_records__branch'
        ).all()

        # ?category=Canned Goods  → filter by category name
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(
                category__category_name__icontains=category
            )

        # ?search=tuna  → search by product name or SKU (handled by filter_backends)
        # ?branch=Tarcan → filter products available at a specific branch
        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(
                inventory_records__branch__branch_name__icontains=branch,
                inventory_records__quantity_on_hand__gt=0
            ).distinct()

        return queryset

    def get_serializer_class(self):
        """
        Use the read serializer for GET requests (has computed fields).
        Use the write serializer for POST/PUT/PATCH (accepts camelCase input).
        """
        if self.action in ['create', 'update', 'partial_update']:
            return ProductWriteSerializer
        return ProductSerializer

    def check_write_permission(self, request):
        """Only Owners and Branch Managers can create/edit/delete products."""
        if request.user.role not in [
            User.Role.OWNER, User.Role.BRANCH_MANAGER
        ]:
            return Response(
                {'detail': 'You do not have permission to modify products.'},
                status=status.HTTP_403_FORBIDDEN
            )
        return None

    def create(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        denied = self.check_write_permission(request)
        if denied:
            return denied
        return super().destroy(request, *args, **kwargs)