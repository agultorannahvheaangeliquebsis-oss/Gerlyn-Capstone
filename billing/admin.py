from django.contrib import admin
from .models import BillingRecord

@admin.register(BillingRecord)
class BillingRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'transaction', 'amount_due', 'payment_method', 'status', 'confirmed_by', 'date_recorded')
    list_filter = ('status', 'payment_method')
    search_fields = ('reference_number', 'transaction__id')