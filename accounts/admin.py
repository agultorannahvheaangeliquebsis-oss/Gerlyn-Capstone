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
    """Where wholesale eligibility gets set, for now.

    Accounts > Manage Users is Owner-only but explicitly excludes CUSTOMER
    rows (accounts.views._user_list_context) and its form can only assign
    internal roles, so there is no in-app page that shows a specific customer
    account to set this on. Until one exists, this is the toggle: listed and
    editable straight from the changelist so it does not need a round trip
    through a detail page.
    """
    list_display = ('__str__', 'user', 'contact_number', 'is_wholesale_eligible')
    list_editable = ('is_wholesale_eligible',)
    list_filter = ('is_wholesale_eligible',)
    search_fields = ('first_name', 'last_name', 'user__username', 'contact_number')


admin.site.register(User, CustomUserAdmin)
admin.site.register(OwnerProfile)
admin.site.register(BranchManagerProfile)
admin.site.register(StaffProfile)
admin.site.register(CustomerProfile, CustomerProfileAdmin)
admin.site.register(SupplierProfile)