"""
core/notifications.py — fan-out helper for the Owner Notification Center.

Every internal-facing event that should surface to the back office goes
through notify() rather than each app creating accounts.models.Notification
rows by hand — one place to get recipients and required fields right.

Recipients, by role:
  Owner           — always, every trigger, every branch (they oversee all of
                    them, same as core.scoping.branch_ids_for_user's None).
  Branch Manager,
  Staff           — `branch`'s Manager and Staff, but ONLY when the call
                    site passes `required_module`, and only the ones who
                    actually have that module enabled
                    (core.module_permissions.user_can_access_module — which
                    already treats Manager and Staff identically; "Only
                    STAFF and BRANCH_MANAGER are subject to this [gating]").
                    Triggers with no `required_module` (role changes, login
                    lockouts, new staff accounts) are Administration-tier
                    and reach the Owner ONLY — not even the branch's own
                    Manager — matching module_permissions.py's own "Branch
                    Management and User Management... stay Owner-only, full
                    stop." A Manager's default module grant is every module
                    (core.module_permissions.default_allowed_modules), so in
                    practice an unrestricted Manager still sees everything
                    module-tied about their branch; this only bites once the
                    Owner has actually narrowed a specific Manager's access.

Deferred imports throughout (matching core/context_processors.py's existing
pattern): core is imported early via INSTALLED_APPS/context processors, so
importing accounts.models or branches.models at module load time risks
running before those apps are ready.
"""
from decimal import Decimal

CATEGORY_STAFF = 'staff'
CATEGORY_PROCUREMENT = 'procurement'
CATEGORY_SALES = 'sales'
CATEGORY_SYSTEM = 'system'

PRIORITY_CRITICAL = 'critical'
PRIORITY_HIGH = 'high'
PRIORITY_MEDIUM = 'medium'
PRIORITY_LOW = 'low'

# Priorities that stay pinned as unresolved until the Owner acts on them,
# rather than clearing the moment they're opened.
_REQUIRES_ACTION = frozenset({PRIORITY_CRITICAL, PRIORITY_HIGH})

# Placeholder branch-wide threshold for the "unusually large order" trigger.
# A per-branch threshold (set from Administration) is the natural next step
# — not built yet, so every branch shares this one constant for now.
LARGE_ORDER_THRESHOLD = Decimal('5000.00')


def _recipients(branch=None, required_module=None):
    """Owner accounts, plus — only when `required_module` is given —
    `branch`'s Manager and Staff who have that module enabled. See the
    module docstring for the reasoning."""
    from django.db.models import Q

    from accounts.models import User
    from core.module_permissions import user_can_access_module
    from core.permissions import BRANCH_MANAGER, OWNER, STAFF

    users = list(User.objects.filter(role=OWNER, is_active=True))
    if branch is None or not required_module:
        return users

    branch_users = User.objects.filter(
        role__in=(BRANCH_MANAGER, STAFF), is_active=True,
    ).filter(
        Q(manager_profile__branch=branch) | Q(staff_profile__branch=branch)
    )
    users += [
        u for u in branch_users if user_can_access_module(u, required_module)
    ]

    return users


def notify(category, priority, title, message, branch=None,
           action_url='', action_label='', required_module=None):
    """
    Create one Notification per recipient (see _recipients). Returns how
    many rows were written.

    `required_module` is one of core.module_permissions' codes (e.g.
    PROCUREMENT, SALES_POS, BILLING, FLOOR_STOCK, DELIVERIES) — pass it when
    the event is something a Manager or Staff member with that module could
    plausibly act on, so it also reaches whichever of the branch's Manager/
    Staff have it enabled. Leave it unset for Administration/security-tier
    events, which reach the Owner only — not even the branch's Manager.

    requires_action is derived from priority (Critical/High), not passed in
    — keeps every call site from having to repeat that policy decision.
    """
    from accounts.models import Notification

    recipients = _recipients(branch, required_module)
    if not recipients:
        return 0

    rows = [
        Notification(
            user=user, title=title, message=message,
            category=category, priority=priority, branch=branch,
            action_url=action_url, action_label=action_label,
            requires_action=priority in _REQUIRES_ACTION,
        )
        for user in recipients
    ]
    Notification.objects.bulk_create(rows)
    return len(rows)


def notify_supplier(supplier, priority, title, message,
                     action_url='', action_label=''):
    """
    One notification straight to a specific Supplier user.

    Unlike notify() above, there's no fan-out to compute: a purchase order
    already has exactly one supplier (Procurement.supplier is a direct FK to
    that User), so this writes one row for that one recipient and nothing
    else — never the Owner or a branch's Manager/Staff, who already learn
    about the same event through their own notify() call at the same call
    site when the trigger warrants it.

    requires_action is always False here: a supplier can't "resolve"
    anything the way the Owner Notification Center means it (there's no
    internal action left for them to take beyond reading), so their tray
    never needs the Resolve workflow — only Dismiss.

    No-ops (returns 0) if `supplier` isn't an active Supplier account —
    Procurement.supplier is nullable (SET_NULL), so a deleted/deactivated
    supplier must not silently raise here.
    """
    from accounts.models import Notification
    from core.permissions import SUPPLIER

    if supplier is None or supplier.role != SUPPLIER or not supplier.is_active:
        return 0

    Notification.objects.create(
        user=supplier, title=title, message=message,
        category=CATEGORY_PROCUREMENT, priority=priority,
        action_url=action_url, action_label=action_label,
        requires_action=False,
    )
    return 1
