from django.contrib import admin

from .models import HomeHeroSlide, StoreSetting


@admin.register(StoreSetting)
class StoreSettingAdmin(admin.ModelAdmin):
    """Single-row config. Edited primarily on the internal Settings page
    (core.views.store_settings_update); registered here as a fallback."""

    def has_add_permission(self, request):
        # The row is created on first access via StoreSetting.load().
        return not StoreSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(HomeHeroSlide)
class HomeHeroSlideAdmin(admin.ModelAdmin):
    list_display = (
        "headline", "placement", "sort_order", "is_active",
        "is_draft", "pending_delete",
    )
    list_filter = ("placement", "is_active", "is_draft")
    list_editable = ("sort_order", "is_active")

    def get_queryset(self, request):
        # Shadow rows are internal snapshots for the Settings-page draft
        # workflow — not something to edit by hand here.
        return super().get_queryset(request).filter(is_shadow=False)
