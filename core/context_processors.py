"""Template context available on every page."""
from core.permissions import INTERNAL_ROLES
from core.scoping import branch_ids_for_user
from core.version import APP_VERSION


def app_version(request):
    return {"APP_VERSION": APP_VERSION}


# Maps the resolved URL name of the current request to the sidebar's
# `active_page` key. Keyed by URL name (not path) so it survives URL edits.
# List, detail and form views of one feature all point at the same key, so the
# sidebar highlights — and the containing dropdown auto-expands — on every page
# of that feature, not just its landing page.
_ACTIVE_PAGE_BY_URL_NAME = {
    # Dashboard (top-level)
    "owner_dashboard": "dashboard",
    "manager_dashboard": "dashboard",
    "staff_dashboard": "dashboard",
    # Inventory
    "product_list": "products", "product_add": "products", "product_edit": "products",
    "product_delete": "products", "product_detail": "products",
    "category_list": "categories", "category_add": "categories",
    "category_edit": "categories", "category_delete": "categories",
    "inventory_list": "stock", "warehouse_stock_list": "stock",
    "transfer_list": "stock_requests", "transfer_create": "stock_requests",
    "transfer_detail": "stock_requests", "transfer_approve": "stock_requests",
    "transfer_complete": "stock_requests", "transfer_confirm_receipt": "stock_requests",
    "transfer_flag_discrepancy": "stock_requests",
    # Procurement
    "supplier_list": "suppliers", "supplier_add": "suppliers",
    "supplier_edit": "suppliers", "supplier_delete": "suppliers",
    "procurement_list": "procurement", "procurement_add": "procurement",
    "procurement_detail": "procurement", "procurement_update_status": "procurement",
    "delivery_list": "deliveries", "delivery_receive": "deliveries",
    "receiving_history": "deliveries",
    # Financials
    "transaction_list": "orders", "transaction_add": "orders",
    "transaction_detail": "orders", "transaction_set_status": "orders",
    "billing_list": "billing", "billing_create": "billing",
    "billing_detail": "billing", "billing_confirm": "billing",
    # System & Monitoring
    "reports_dashboard": "reports", "sales_report": "reports",
    "inventory_report": "reports", "procurement_report": "reports",
    "billing_report": "reports", "export_csv": "reports",
    "alerts_center": "alerts",
    # Administration
    "branch_list": "branches", "branch_add": "branches",
    "branch_edit": "branches", "branch_delete": "branches",
    "user_list": "users", "user_add": "users",
    "user_edit": "users", "user_toggle_active": "users",
}


def active_nav(request):
    """
    Expose `active_page` for the sidebar on every internal page.

    Derived from the resolved URL name so the highlight and the auto-expanding
    dropdown work across list/detail/form views without each view having to set
    it. A view may still pass its own `active_page` in context to override this.
    Fail-safe: never raises, just yields no active page.
    """
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", None) if match else None
    page = _ACTIVE_PAGE_BY_URL_NAME.get(url_name)
    return {"active_page": page} if page else {}


def nav_categories(request):
    """
    Expose `nav_categories` for the storefront header's quick-link row.

    The row sits in public_base.html, which cart, checkout, product detail and
    the order pages all extend — none of which were passing categories of their
    own, so the row would have rendered empty on most of the shop.

    Costs nothing on internal pages: a sliced QuerySet is lazy, and no internal
    template names this variable, so the query is never executed there.
    """
    from inventory.models import ProductCategory

    return {"nav_categories": ProductCategory.objects.all()[:8]}


ALERT_COUNT_CACHE_SECONDS = 30


def alerts(request):
    """
    Expose `alert_count` for the sidebar/bottom-nav badge.

    Branch-scoped: a manager sees only their own branch's alert count.
    Kept cheap and fail-safe — a badge must never break a page render.

    Cached per-user for ALERT_COUNT_CACHE_SECONDS: this runs on every single
    internal page render (it's a context processor), but stock levels don't
    change every second, so re-running the low_stock_alerts join on every
    nav click was pure latency with nothing to show for it. A stale badge
    for up to 30s is a fine trade against paying a DB round trip per click.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    if getattr(user, "role", None) not in INTERNAL_ROLES:
        return {}

    from django.core.cache import cache

    cache_key = f"alert_count:{user.id}"
    count = cache.get(cache_key)
    if count is not None:
        return {"alert_count": count}

    try:
        from inventory.alerts import low_stock_alerts

        count = len(low_stock_alerts(branch_ids_for_user(user)))
    except Exception:
        return {}

    cache.set(cache_key, count, ALERT_COUNT_CACHE_SECONDS)
    return {"alert_count": count}
