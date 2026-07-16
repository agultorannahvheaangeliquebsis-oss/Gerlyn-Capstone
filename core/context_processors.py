"""Template context available on every page."""
from core.permissions import INTERNAL_ROLES
from core.scoping import branch_ids_for_user


def alerts(request):
    """
    Expose `alert_count` for the sidebar/bottom-nav badge.

    Branch-scoped: a manager sees only their own branch's alert count.
    Kept cheap and fail-safe — a badge must never break a page render.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    if getattr(user, "role", None) not in INTERNAL_ROLES:
        return {}

    try:
        from inventory.alerts import low_stock_alerts

        return {"alert_count": len(low_stock_alerts(branch_ids_for_user(user)))}
    except Exception:
        return {}
