from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only: the log can be looked at here, never changed."""
    list_display = ('timestamp', 'actor_id', 'action', 'entity', 'record_id', 'summary')
    list_filter = ('action', 'entity')
    search_fields = ('record_id', 'record_repr', 'summary')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
