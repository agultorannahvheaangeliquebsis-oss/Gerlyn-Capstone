from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    User, OwnerProfile, BranchManagerProfile,
    StaffProfile, CustomerProfile, SupplierProfile, Notification,
    PermissionChangeLog,
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


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Audit view of the event/recipient matrix fan-out."""

    list_display = (
        'event_type', 'user', 'recipient_role', 'created_at', 'is_read',
        'read_at', 'reference', 'action_url',
    )
    list_filter = ('event_type', 'recipient_role', 'is_read', 'priority', 'category')
    search_fields = (
        'title', 'message', 'user__username', 'reference_type', 'reference_id',
    )
    readonly_fields = tuple(field.name for field in Notification._meta.fields)
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Reference')
    def reference(self, obj):
        if not obj.reference_type:
            return '-'
        return f'{obj.reference_type}:{obj.reference_id}'


@admin.register(PermissionChangeLog)
class PermissionChangeLogAdmin(admin.ModelAdmin):
    list_display = ('target_user', 'changed_by', 'changed_at')
    readonly_fields = tuple(field.name for field in PermissionChangeLog._meta.fields)
    date_hierarchy = 'changed_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(User, CustomUserAdmin)
admin.site.register(OwnerProfile)
admin.site.register(BranchManagerProfile)
admin.site.register(StaffProfile)
admin.site.register(CustomerProfile, CustomerProfileAdmin)
admin.site.register(SupplierProfile)
