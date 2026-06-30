from django.contrib import admin
from .models import ProductCategory, Product, Inventory

@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ('category_name', 'description')
    search_fields = ('category_name',)

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'sku_code', 'category', 'unit_of_measure', 'unit_price', 'wholesale_price')
    search_fields = ('product_name', 'sku_code')
    list_filter = ('category', 'unit_of_measure')

@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ('product', 'branch', 'quantity_on_hand', 'reorder_level', 'restock', 'last_stock_update')
    list_filter = ('branch', 'restock')
    search_fields = ('product__product_name',)