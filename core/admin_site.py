"""Django admin restricted to genuine superusers.

The stock AdminSite admits any active ``is_staff`` user and then defers to
per-model permissions. GerlynPLUS business accounts (Owner, Manager, Staff,
Supplier, Customer) must never reach it merely because of an application role,
so this site requires ``is_superuser`` as well. Application roles live in
``User.role`` and are deliberately unrelated to ``is_staff``/``is_superuser``.

Wired in through INSTALLED_APPS (``core.admin_site.SuperuserOnlyAdminConfig``
replaces ``django.contrib.admin``), so every ``admin.site`` / ``@admin.register``
in the project keeps working unchanged.
"""
from django.contrib import admin
from django.contrib.admin.apps import AdminConfig


class SuperuserOnlyAdminSite(admin.AdminSite):
    def has_permission(self, request):
        user = request.user
        return bool(user.is_active and user.is_staff and user.is_superuser)


class SuperuserOnlyAdminConfig(AdminConfig):
    default_site = 'core.admin_site.SuperuserOnlyAdminSite'
