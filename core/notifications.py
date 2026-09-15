"""
core/notifications.py — fan-out helper for the Owner Notification Center.

Every internal-facing event that should surface to the back office goes
through notify() rather than each app creating accounts.models.Notification
rows by hand — one place to get recipients and required fields right.

Recipients, by role:
  Owner           — always, every trigger, every branch (they oversee all of
                    them, same as core.scoping.branch_ids_for_user's None).
  Branch Manager  — `branch`'s Manager, on every trigger for that branch.
                    A Manager already sees everything about their own branch
                    elsewhere in this app (core.scoping), so no extra gate.
  Staff           — `branch`'s Staff, but ONLY when the call site passes
                    `required_module`, and only the Staff who actually have
                    that module enabled (core.module_permissions). This
                    mirrors the module system's own rule: Staff visibility is
                    opt-in per section, granted per employee by the Owner —
                    never a role-wide default. Triggers with no
                    `required_module` (role changes, login lockouts, new
                    staff accounts) are Administration-tier and stay
                    Owner/Manager-only, matching module_permissions.py's own
                    "Branch Management and User Management... stay
                    Owner-only, full stop."

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
    """Owner accounts, plus `branch`'s Branch Manager, plus — only when
    `required_module` is given — that branch's Staff who have that module
    enabled. See the module docstring for the reasoning."""
    from accounts.models import User
    from core.module_permissions import user_can_access_module
    from core.permissions import BRANCH_MANAGER, OWNER, STAFF

    users = list(User.objects.filter(role=OWNER, is_active=True))
    if branch is None:
        return users

    users += list(
        User.objects.filter(
            role=BRANCH_MANAGER, is_active=True,
            manager_profile__branch=branch,
        )
    )

    if required_module:
        staff = User.objects.filter(
            role=STAFF, is_active=True, staff_profile__branch=branch,
        )
        users += [
            u for u in staff if user_can_access_module(u, required_module)
        ]

    return users


def notify(category, priority, title, message, branch=None,
           action_url='', action_label='', required_module=None):
    """
    Create one Notification per recipient (see _recipients). Returns how
    many rows were written.

    `required_module` is one of core.module_permissions' codes (e.g.
    PROCUREMENT, SALES_POS, BILLING, FLOOR_STOCK, DELIVERIES) — pass it when
    the event is something a Staff member with that module could plausibly
    act on, so it also reaches branch Staff who have it enabled. Leave it
    unset for Administration/security-tier events, which stay Owner/Manager
    only regardless of any Staff account's module grants.

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
