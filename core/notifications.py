"""
core/notifications.py — fan-out helper for the Owner Notification Center.

Every internal-facing event that should surface to the Owner (and, when it's
branch-specific, that branch's Manager) goes through notify() rather than
each app creating accounts.models.Notification rows by hand — one place to
get recipients and required fields right.

Recipients: the Owner always gets notified (they oversee every branch).
Pass `branch` to also notify that branch's Manager. Deliberately excludes
Staff — the categories here (large orders, PO status, role changes, security
events) are Owner/Manager-level decisions, matching how alerts_center and
procurement approvals are already scoped in this codebase.

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


def _recipients(branch=None):
    """Owner accounts, plus `branch`'s Branch Manager if one is assigned."""
    from accounts.models import User
    from core.permissions import BRANCH_MANAGER, OWNER

    users = list(User.objects.filter(role=OWNER, is_active=True))
    if branch is not None:
        users += list(
            User.objects.filter(
                role=BRANCH_MANAGER, is_active=True,
                manager_profile__branch=branch,
            )
        )
    return users


def notify(category, priority, title, message, branch=None,
           action_url='', action_label=''):
    """
    Create one Notification per recipient (Owner + that branch's Manager,
    see _recipients). Returns how many rows were written.

    requires_action is derived from priority (Critical/High), not passed in
    — keeps every call site from having to repeat that policy decision.
    """
    from accounts.models import Notification

    recipients = _recipients(branch)
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
