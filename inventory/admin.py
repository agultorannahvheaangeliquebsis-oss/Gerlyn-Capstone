from django.contrib import admin
from .models import (
    ProductCategory, Product, ProductImage, ProductPriceTier,
    Inventory, StockMovement,
)


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ('category_name', 'description')
    search_fields = ('category_name',)


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ('image', 'alt_text', 'is_primary')


class ProductPriceTierInline(admin.TabularInline):
    model = ProductPriceTier
    extra = 3
    fields = ('min_quantity', 'unit_price')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        'product_name', 'sku_code', 'category',
        'unit_of_measure', 'unit_price', 'bulk_tiers'
    )
    search_fields = ('product_name', 'sku_code')
    list_filter = ('category', 'unit_of_measure')
    inlines = [ProductImageInline, ProductPriceTierInline]

    @admin.display(description='Bulk tiers')
    def bulk_tiers(self, obj):
        tiers = obj.price_tiers.all()
        return ', '.join(f'{t.min_quantity}+ @ {t.unit_price}' for t in tiers) or '—'


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'branch', 'quantity_on_hand',
        'reorder_level', 'restock', 'last_stock_update'
    )
    list_filter = ('branch', 'restock')
    search_fields = ('product__product_name',)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'inventory', 'movement_type',
        'quantity_before', 'quantity_change', 'quantity_after',
        'reference_id', 'created_by'
    )
    list_filter = ('movement_type', 'inventory__branch')
    search_fields = ('inventory__product__product_name',)
    readonly_fields = (
        'inventory', 'movement_type', 'quantity_change',
        'quantity_before', 'quantity_after', 'reference_id',
        'notes', 'created_by', 'created_at'
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False