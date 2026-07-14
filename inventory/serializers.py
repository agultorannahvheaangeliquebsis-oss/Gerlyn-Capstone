from rest_framework import serializers
from .models import Product, ProductCategory, Inventory, Warehouse, WarehouseStock


class ProductCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ['id', 'category_name', 'description']


class ProductSerializer(serializers.ModelSerializer):
    # ── Field name translations (Django → React camelCase) ──────────────────
    # React expects: sku, name, unit, retailPrice, wholesalePrice, category (string)
    # Django stores: sku_code, product_name, unit_of_measure, unit_price, wholesale_price, category (FK)

    sku = serializers.CharField(source='sku_code')
    name = serializers.CharField(source='product_name')
    unit = serializers.CharField(source='unit_of_measure')
    retailPrice = serializers.DecimalField(
        source='unit_price', max_digits=10, decimal_places=2
    )
    wholesalePrice = serializers.DecimalField(
        source='wholesale_price', max_digits=10, decimal_places=2,
        allow_null=True, required=False
    )

    # ── Nested / computed fields ──────────────────────────────────────────────
    # React expects category as a plain string (e.g. "Canned Goods")
    # Django stores it as a FK — we resolve the name here
    category = serializers.SerializerMethodField()

    # React expects availability as an array of branch name strings
    # e.g. ["Tarcan", "Makinabang"]
    # Django derives this from Inventory records — computed here
    availability = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id',
            'sku',
            'name',
            'category',
            'unit',
            'retailPrice',
            'wholesalePrice',
            'description',
            'availability',
        ]

    def get_category(self, obj):
        """Return the category name string, not an integer FK."""
        return obj.category.category_name if obj.category else None

    def get_availability(self, obj):
        """
        Return the list of branch names where this product
        has at least 1 unit of floor stock (Inventory records).
        React expects: ["Tarcan", "Makinabang"]
        """
        return list(
            obj.inventory_records
            .filter(quantity_on_hand__gt=0)
            .values_list('branch__branch_name', flat=True)
        )


class ProductWriteSerializer(serializers.ModelSerializer):
    """
    Separate serializer for POST/PUT/PATCH requests.
    Accepts camelCase from React and writes to Django snake_case fields.
    We use a write serializer because the read serializer
    has computed fields that are not writable.
    """
    sku = serializers.CharField(source='sku_code')
    name = serializers.CharField(source='product_name')
    unit = serializers.CharField(source='unit_of_measure')
    retailPrice = serializers.DecimalField(
        source='unit_price', max_digits=10, decimal_places=2
    )
    wholesalePrice = serializers.DecimalField(
        source='wholesale_price', max_digits=10, decimal_places=2,
        allow_null=True, required=False
    )
    categoryId = serializers.PrimaryKeyRelatedField(
        source='category', queryset=ProductCategory.objects.all()
    )

    class Meta:
        model = Product
        fields = [
            'sku',
            'name',
            'categoryId',
            'unit',
            'retailPrice',
            'wholesalePrice',
            'description',
        ]