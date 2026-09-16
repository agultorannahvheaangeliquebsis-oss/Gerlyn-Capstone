from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    User, OwnerProfile, BranchManagerProfile,
    StaffProfile, CustomerProfile, SupplierProfile
)


class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Role', {'fields': ('role',)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Role', {'fields': ('role',)}),
    )


class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'user', 'contact_number')
    search_fields = ('first_name', 'last_name', 'user__username', 'contact_number')


admin.site.register(User, CustomUserAdmin)
admin.site.register(OwnerProfile)
admin.site.register(BranchManagerProfile)
admin.site.register(StaffProfile)
admin.site.register(CustomerProfile, CustomerProfileAdmin)
admin.site.register(SupplierProfile)