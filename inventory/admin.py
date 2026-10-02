from django.contrib import admin
from .models import (
    ProductCategory, Product, ProductImage, ProductPriceTier,
    Inventory, StockMovement, OpeningStockImport, OpeningStockImportRow,
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
    fields = (
        'product', 'branch', 'quantity_on_hand', 'reorder_level',
        'restock', 'last_stock_update',
    )
    readonly_fields = (
        'product', 'branch', 'quantity_on_hand', 'restock', 'last_stock_update',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'inventory', 'movement_type',
        'quantity_before', 'quantity_change', 'quantity_after',
        'batch', 'reference_id', 'created_by'
    )
    list_filter = ('movement_type', 'inventory__branch')
    search_fields = (
        'inventory__product__product_name', 'warehouse_stock__product__product_name',
        'batch__batch_number', 'batch__external_lot_code',
    )
    readonly_fields = (
        'inventory', 'movement_type', 'quantity_change',
        'quantity_before', 'quantity_after', 'batch', 'reference_id',
        'notes', 'created_by', 'created_at'
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OpeningStockImportRowInline(admin.TabularInline):
    model = OpeningStockImportRow
    extra = 0
    can_delete = False
    readonly_fields = (
        'row_number', 'product', 'branch', 'quantity', 'external_lot_code',
        'received_date', 'expiry_date', 'previous_quantity',
        'resulting_quantity', 'reversed_quantity', 'consumed_quantity',
    )


@admin.register(OpeningStockImport)
class OpeningStockImportAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'original_filename', 'as_of_date', 'row_count',
        'total_quantity', 'status', 'uploaded_by', 'confirmed_at',
    )
    list_filter = ('status', 'as_of_date')
    search_fields = ('original_filename', 'raw_sha256', 'canonical_sha256')
    readonly_fields = [field.name for field in OpeningStockImport._meta.fields]
    inlines = [OpeningStockImportRowInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
