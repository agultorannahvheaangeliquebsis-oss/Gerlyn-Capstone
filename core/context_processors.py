"""Template context available on every page."""
from core.module_permissions import module_access_map
from core.permissions import INTERNAL_ROLES
from core.scoping import branch_ids_for_user
from core.version import APP_VERSION


def app_version(request):
    return {"APP_VERSION": APP_VERSION}


def module_access(request):
    """Exposes `module_access.<code>` for the sidebar (and anywhere else
    that needs to hide a link to a section a Staff/Manager account can't
    see — see core.module_permissions). Owner/Supplier/Customer always read
    as True for every code, so templates don't need a role check alongside
    this one."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {"module_access": module_access_map(user)}


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
    "inventory_list": "stock", "warehouse_stock_list": "warehouse_stock",
    "transfer_list": "withdrawals", "transfer_create": "withdrawals",
    "transfer_detail": "withdrawals", "transfer_approve": "withdrawals",
    "transfer_complete": "withdrawals", "transfer_confirm_receipt": "withdrawals",
    "transfer_flag_discrepancy": "withdrawals",
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

    Empty while the store has fewer than two categories, which is what every
    storefront surface keys its category UI off — the chips under the search,
    the Shop menu, the footer column, the home page tiles, the catalog's
    Category filter and the line on each product card.

    A lone category is not a choice. With one row in the table ("General",
    holding every product), the chip row offered a filter that filters
    nothing, the rail asked the shopper to pick between "All products" and
    the only category there is, and every card in the grid carried the same
    word. None of that told anyone anything; it just spent space saying
    "General" six times per screen. The moment a second category exists all
    of it comes back on its own — this is a threshold, not a deletion, and
    nothing about the category is removed from the database or the internal
    system.

    Costs nothing on internal pages: SimpleLazyObject defers the query until
    a template actually reads the variable, and no internal template names
    it, so it is never executed there.
    """
    from django.utils.functional import SimpleLazyObject

    def _categories():
        from inventory.models import ProductCategory

        cats = list(ProductCategory.objects.all()[:8])
        return cats if len(cats) > 1 else []

    return {"nav_categories": SimpleLazyObject(_categories)}


def nav_branches(request):
    """
    Expose `nav_branches` for the storefront footer's Visit Us column.

    Same reasoning as nav_categories: the footer lives in public_base.html,
    which most of the shop extends, and none of those views pass branches.
    Listing them from the database rather than hardcoding three names is what
    keeps the footer from quietly lying the day a fourth branch opens.

    Lazy QuerySet, so internal pages — which never name this variable — pay
    nothing for it.
    """
    from branches.models import Branch

    return {"nav_branches": Branch.objects.all()}


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


def nav_selected_branch(request):
    """
    Expose `nav_selected_branch` for the storefront nav bar's status line.

    The design reference puts a standing promise ("orders processed within 24
    hours") in that slot. This store has no such SLA recorded anywhere, and a
    delivery promise invented in a template is a claim the business never
    made — so the slot carries the one fact the session actually knows: which
    branch the shopper is buying from, with a link to change it when they are
    not on one yet.

    Only queries when a branch has been chosen, so staff pages and first-time
    visitors pay nothing. Fail-safe: a nav bar must never break a render.
    """
    branch_id = request.session.get('branch_id') if hasattr(request, 'session') else None
    if not branch_id:
        return {}
    try:
        from branches.models import Branch

        return {"nav_selected_branch": Branch.objects.filter(pk=branch_id).first()}
    except Exception:
        return {}


def customer_nav(request):
    """
    Expose `cart_count` and `notification_count` for the storefront header's
    icon row (bell · account · cart).

    Both used to be per-view context: cart_count was passed by seven views in
    orders/customer_views.py and by nothing else, so the badge on the cart
    button simply vanished the moment a shopper stepped onto an account page
    — the same header, the same session, a different number. A header that
    lives in public_base.html needs its numbers from something that runs on
    every render of public_base.html, which is this.

    Those views still pass their own cart_count and still win: view context
    overrides a context processor, and the value is identical either way.

    Fail-safe on both counts — a badge must never break a page render.
    """
    out = {}

    session = getattr(request, "session", None)
    if session is not None:
        try:
            cart = session.get("cart", {})
            out["cart_count"] = len(cart)
            # No cart_product_ids any more. It existed so a card could paint
            # its Add-to-cart button in the added state on first load; the
            # tick is now a two-second receipt for a press (see toast.js
            # markAdded), and painting one on load would have a card take
            # credit for an add made on some earlier page. The badge above is
            # what says what the cart holds.
        except Exception:
            pass

    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        try:
            out["notification_count"] = user.notifications.filter(
                is_read=False
            ).count()
        except Exception:
            pass

    return out
