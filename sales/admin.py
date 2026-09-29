from django.contrib import admin
from .models import PosSalesImport, SalesTransaction, SalesItem


class SalesItemInline(admin.TabularInline):
    model = SalesItem
    extra = 1
    readonly_fields = ('subtotal',)


@admin.register(SalesTransaction)
class SalesTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'branch', 'staff', 'customer',
        'transaction_date', 'total_amount',
        'transaction_type', 'mode_of_payment'
    )
    list_filter = ('branch', 'transaction_type', 'mode_of_payment')
    search_fields = ('id', 'customer__username', 'staff__username')
    inlines = [SalesItemInline]


@admin.register(SalesItem)
class SalesItemAdmin(admin.ModelAdmin):
    list_display = ('transaction', 'product', 'quantity_sold', 'unit_price', 'subtotal')
    readonly_fields = ('subtotal',)


@admin.register(PosSalesImport)
class PosSalesImportAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'original_filename', 'branch', 'imported_at', 'imported_by',
        'successful_transactions', 'skipped_duplicates', 'failed_rows',
    )
    readonly_fields = [field.name for field in PosSalesImport._meta.fields]
