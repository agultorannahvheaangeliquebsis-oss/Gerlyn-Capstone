from django.contrib import admin
from .models import Branch


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ('branch_name', 'location', 'is_active', 'latitude', 'longitude')
    list_filter = ('is_active', 'city', 'barangay')
    search_fields = ('branch_name', 'location', 'street_detail')
    fieldsets = (
        (None, {'fields': ('owner', 'branch_name', 'location', 'is_active')}),
        ('Service area', {'fields': ('region', 'province', 'city', 'barangay', 'street_detail')}),
        ('Fulfillment coordinates', {
            'fields': ('latitude', 'longitude'),
            'description': 'Used only to calculate approximate straight-line distance for automatic fulfillment.',
        }),
    )
