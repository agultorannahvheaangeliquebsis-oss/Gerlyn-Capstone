from django.contrib import admin
from .models import Procurement, ProcurementItem


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
        'quantity_ordered', 'quantity_received', 'cost_price'
    )