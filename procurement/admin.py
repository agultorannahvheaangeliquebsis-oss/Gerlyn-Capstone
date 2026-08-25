from django.contrib import admin
from .models import (
    Procurement, ProcurementItem, SupplierProduct, DeliveryScanLog,
)


class ProcurementItemInline(admin.TabularInline):
    model = ProcurementItem
    extra = 1


@admin.register(Procurement)
class ProcurementAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'branch', 'supplier', 'owner',
        'order_date', 'delivery_date',
        'total_cost', 'status'
    )
    list_filter = ('branch', 'status')
    search_fields = ('id', 'supplier__username', 'owner__username')
    inlines = [ProcurementItemInline]


@admin.register(ProcurementItem)
class ProcurementItemAdmin(admin.ModelAdmin):
    list_display = (
        'procurement', 'product',
        'quantity_ordered', 'quantity_received', 'cost_price',
        'is_unexpected',
    )
    list_filter = ('is_unexpected',)


@admin.register(SupplierProduct)
class SupplierProductAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'supplier', 'supplier_sku',
        'default_cost_price', 'lead_time_days', 'is_preferred',
    )
    list_filter = ('is_preferred', 'supplier')
    search_fields = ('product__product_name', 'product__sku_code', 'supplier_sku')
    autocomplete_fields = ()


@admin.register(DeliveryScanLog)
class DeliveryScanLogAdmin(admin.ModelAdmin):
    list_display = (
        'scanned_at', 'procurement', 'scanned_code',
        'matched_product', 'was_unexpected', 'scanned_by',
    )
    list_filter = ('was_unexpected',)
    search_fields = ('scanned_code', 'procurement__po_number')
    # An audit trail is worth nothing if it can be edited after the fact.
    readonly_fields = (
        'procurement', 'procurement_item', 'scanned_code', 'matched_product',
        'was_unexpected', 'scanned_by', 'scanned_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False