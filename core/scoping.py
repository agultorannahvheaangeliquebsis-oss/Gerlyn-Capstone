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


def branch_for_user(user):
    """
    The single Branch a manager/staff is pinned to, or None.

    DO NOT USE THIS TO FILTER A QUERYSET. None is returned for TWO different
    situations that must not be treated alike:

        * the Owner            -> sees ALL branches
        * a Manager/Staff whose profile branch is NULL -> must see NOTHING

    That second case is reachable in normal operation: BranchManagerProfile
    .branch and StaffProfile.branch are both on_delete=SET_NULL, so deleting
    a branch nulls them out. The idiom

        user_branch = branch_for_user(user)
        if user_branch:                      # WRONG - fails OPEN
            qs = qs.filter(branch=user_branch)

    silently hands that account every branch's data. Use `scope_queryset()`
    (or `branch_ids_for_user()` when you need the ids) instead — those
    return None only for the Owner and [] for an unassigned account, so they
    fail CLOSED.

    This function is for the cases that genuinely want the Branch object and
    treat None as "ask which branch": a form default, a filename label, a
    "should I show the Owner's branch picker" check.
    """
    if not getattr(user, "is_authenticated", False):
        return None

    role = getattr(user, "role", None)
    if role == BRANCH_MANAGER:
        profile = getattr(user, "manager_profile", None)
        return profile.branch if profile else None
    if role == STAFF:
        profile = getattr(user, "staff_profile", None)
        return profile.branch if profile else None
    return None


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
