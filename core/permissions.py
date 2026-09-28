"""
core/permissions.py — Role-Based Access Control (RBAC) helpers.

Single source of truth for "who can do what". Used by both Django views
(server-rendered templates) and Django REST Framework API endpoints.

Roles (from accounts.User.Role):
    OWNER          — sees ALL branches, sole procurement authority, admin
    BRANCH_MANAGER — own branch only: monitoring, reports, approvals
    STAFF          — own branch only: POS, inventory, deliveries
    SUPPLIER       — own purchase orders / deliveries
    CUSTOMER       — storefront: place & track orders

See gerlyn-one-week-build-plan.md §1 for the full permission matrix.
"""
from functools import wraps

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from rest_framework.permissions import BasePermission

# --- Role constants (import these instead of hard-coding strings) -----------
OWNER = "OWNER"
BRANCH_MANAGER = "BRANCH_MANAGER"
STAFF = "STAFF"
SUPPLIER = "SUPPLIER"
CUSTOMER = "CUSTOMER"

# Roles that operate the internal system (back office).
INTERNAL_ROLES = frozenset({OWNER, BRANCH_MANAGER, STAFF})


def user_has_role(user, roles) -> bool:
    """True if `user` is authenticated and holds one of `roles`."""
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "role", None) in set(roles)
    )


# =============================================================================
# Django class-based views
# =============================================================================
class RoleRequiredMixin(LoginRequiredMixin):
    """
    Mixin for Django CBVs. Set `allowed_roles` on the view.

        class OwnerReportView(RoleRequiredMixin, TemplateView):
            allowed_roles = (OWNER,)
    """

    allowed_roles: tuple = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(settings.LOGIN_URL)
        if not self.allowed_roles or request.user.role not in self.allowed_roles:
            raise PermissionDenied(
                "Your role does not have access to this page."
            )
        return super().dispatch(request, *args, **kwargs)


# =============================================================================
# Django function-based views
# =============================================================================
def role_required(*roles):
    """
    Decorator for function views.

        @role_required(OWNER, BRANCH_MANAGER)
        def branch_report(request): ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(settings.LOGIN_URL)
            if not roles or request.user.role not in roles:
                raise PermissionDenied(
                    "Your role does not have access to this action."
                )
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


# =============================================================================
# Django REST Framework
# =============================================================================
class HasRole(BasePermission):
    """
    DRF permission. Reads `allowed_roles` off the view (or subclass default).

        class ProductViewSet(ModelViewSet):
            permission_classes = [HasRole]
            allowed_roles = (OWNER, BRANCH_MANAGER, STAFF)
    """

    allowed_roles: tuple = ()

    def has_permission(self, request, view):
        roles = getattr(view, "allowed_roles", None) or self.allowed_roles
        return user_has_role(request.user, roles)


def role_permission(*roles):
    """Factory: build a DRF permission class locked to `roles`."""

    class _RolePermission(BasePermission):
        def has_permission(self, request, view):
            return user_has_role(request.user, roles)

    _RolePermission.__name__ = "Is" + "Or".join(r.title() for r in roles)
    return _RolePermission


class IsOwner(BasePermission):
    def has_permission(self, request, view):
        return user_has_role(request.user, {OWNER})


class IsInternal(BasePermission):
    """Any back-office role (Owner, Manager, Staff)."""

    def has_permission(self, request, view):
        return user_has_role(request.user, INTERNAL_ROLES)


class HasModuleAccess(BasePermission):
    """Apply the same per-user action grants to DRF API routes."""

    def has_permission(self, request, view):
        from core.module_permissions import (
            PROTECTED_URL_NAMES, permission_for_request, role_allows,
            route_role_allows, user_can,
        )

        match = getattr(request, 'resolver_match', None)
        url_name = match.url_name if match else None
        if getattr(request.user, 'role', None) not in {BRANCH_MANAGER, STAFF}:
            return True
        if url_name not in PROTECTED_URL_NAMES:
            return True
        permission = permission_for_request(url_name, request.method)
        if permission is None:
            return True
        if not route_role_allows(url_name, request.user.role):
            return True
        if permission and not role_allows(request.user.role, permission[0], permission[1]):
            return False
        return bool(permission and user_can(request.user, permission[0], permission[1]))
