from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .forms import AccountAdminChangeForm, AccountAdminCreationForm
from .models import (
    User, OwnerProfile, BranchManagerProfile,
    StaffProfile, CustomerProfile, SupplierProfile, Notification,
)


class CustomUserAdmin(UserAdmin):
    form = AccountAdminChangeForm
    add_form = AccountAdminCreationForm
    fieldsets = UserAdmin.fieldsets + (
        ('Role', {'fields': ('role',)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Account details', {'fields': ('role', 'first_name', 'last_name', 'email')}),
    )


class ProfileInputAdmin(admin.ModelAdmin):
    class Media:
        js = ('js/input-limits.js',)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if field is None:
            return field
        personal_names = ('first_name', 'last_name', 'owner_name', 'manager_name', 'staff_name', 'contact_person')
        if db_field.name in personal_names:
            field.widget.attrs.update({
                'pattern': r"(?=.*\p{L})[\p{L}\p{M} .'\u2019\-]+",
                'data-account-input': db_field.name,
            })
        if db_field.name == 'contact_number':
            field.widget.attrs.update({
                'pattern': r'(?:0|\+63)[\s\-]*9(?:[\s\-]*[0-9]){9}[\s\-]*',
                'data-account-input': 'contact_number',
            })
        if db_field.name == 'address':
            field.widget.attrs['maxlength'] = 500
        field.widget.attrs['data-label'] = str(db_field.verbose_name).capitalize()
        return field


class CustomerProfileAdmin(ProfileInputAdmin):
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


admin.site.register(User, CustomUserAdmin)
admin.site.register(OwnerProfile, ProfileInputAdmin)
admin.site.register(BranchManagerProfile, ProfileInputAdmin)
admin.site.register(StaffProfile, ProfileInputAdmin)
admin.site.register(CustomerProfile, CustomerProfileAdmin)
admin.site.register(SupplierProfile, ProfileInputAdmin)
