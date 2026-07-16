"""
core/scoping.py — Multi-branch data scoping.

The #1 documented risk (Chapter 3 risk assessment) is that multi-branch
integration leaks data across branches. This module is the guard:

    OWNER          -> sees ALL branches
    BRANCH_MANAGER -> only their assigned branch
    STAFF          -> only their assigned branch
    SUPPLIER/CUSTOMER -> no back-office branch access

Use `scope_queryset()` in views, or `BranchScopedQuerysetMixin` on CBVs.
Always filter server-side — never rely on the UI's branch dropdown alone.
"""
from core.permissions import BRANCH_MANAGER, OWNER, STAFF


def branch_ids_for_user(user):
    """
    Return the branch ids a user may access.

    Returns:
        None  -> ALL branches (Owner). Caller must NOT filter.
        list  -> the specific branch ids allowed (may be empty).
    """
    if not getattr(user, "is_authenticated", False):
        return []

    role = getattr(user, "role", None)

    if role == OWNER:
        return None  # unrestricted

    if role == BRANCH_MANAGER:
        profile = getattr(user, "manager_profile", None)
        return [profile.branch_id] if profile and profile.branch_id else []

    if role == STAFF:
        profile = getattr(user, "staff_profile", None)
        return [profile.branch_id] if profile and profile.branch_id else []

    # Supplier / Customer have no branch-scoped back-office access.
    return []


def scope_queryset(qs, user, branch_field="branch"):
    """
    Filter `qs` to the branches `user` may see.

        transactions = scope_queryset(
            SalesTransaction.objects.all(), request.user
        )

    `branch_field` is the lookup path to the Branch FK (e.g. "warehouse__branch").
    """
    ids = branch_ids_for_user(user)
    if ids is None:
        return qs  # Owner: everything
    return qs.filter(**{f"{branch_field}__in": ids})


def user_can_access_branch(user, branch_id) -> bool:
    """True if `user` may act on the given branch."""
    ids = branch_ids_for_user(user)
    return ids is None or branch_id in ids


class BranchScopedQuerysetMixin:
    """
    CBV mixin. Set `branch_field` if the Branch FK isn't named "branch".

        class InventoryListView(BranchScopedQuerysetMixin, ListView):
            model = Inventory        # branch_field defaults to "branch"
    """

    branch_field = "branch"

    def get_queryset(self):
        qs = super().get_queryset()
        return scope_queryset(qs, self.request.user, self.branch_field)
