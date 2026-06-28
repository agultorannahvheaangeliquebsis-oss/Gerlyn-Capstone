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


admin.site.register(User, CustomUserAdmin)
admin.site.register(OwnerProfile)
admin.site.register(BranchManagerProfile)
admin.site.register(StaffProfile)
admin.site.register(CustomerProfile)
admin.site.register(SupplierProfile)