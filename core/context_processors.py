"""Template context available on every page."""
from django.conf import settings

from core.module_permissions import module_access_map, module_action_map
from core.permissions import INTERNAL_ROLES
from core.scoping import branch_ids_for_user
from core.version import APP_VERSION


def app_version(request):
    return {"APP_VERSION": APP_VERSION}


def map_config(request):
    """Public attribution text only; provider URLs and keys remain server-side."""
    return {"MAP_TILE_ATTRIBUTION": settings.MAP_TILE_ATTRIBUTION}


def store_settings(request):
    """Expose `store_settings` — the single StoreSetting row — on every page.

    Templates that print a document (the purchase order, receipts) read the
    store identity off this, and anywhere that shows an inventory threshold
    can read the live number instead of restating a hardcoded one. One
    cached row (see core.models.StoreSetting.load), so this costs a cache
    hit per render, not a query.
    """
    from core.models import StoreSetting

    return {"store_settings": StoreSetting.load()}


def detail_modal(request):
    """Which shell a detail page wears.

    Every list's "see" action opens the record over the list instead of
    navigating to it (detail-modal.js), and what it drops into the overlay is
    the detail page's own content block — the same view, the same template,
    the same branch checks — asked for with ?modal=1.

    Resolving it here rather than in each view means a detail page opts in by
    extending `base_template` instead of internal_base.html by name, and
    nothing else about the view changes. Without the flag it renders as the
    full page it always was, which is what a bookmark, a direct link, a
    middle-click and a browser with JavaScript off all still get.
    """
    if request.GET.get("modal") == "1":
        return {"base_template": "partials/_modal_base.html"}
    return {"base_template": "internal_base.html"}


def module_access(request):
    """Exposes `module_access.<code>` for the sidebar (and anywhere else
    that needs to hide a link to a section a Staff/Manager account can't
    see — see core.module_permissions). Owner/Supplier/Customer always read
    as True for every code, so templates don't need a role check alongside
    this one."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {
        "module_access": module_access_map(user),
        "module_actions": module_action_map(user),
    }


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
    "supplier_return_list": "returns", "supplier_return_detail": "returns",
    "supplier_return_add": "returns",
    "supplier_category_list": "suppliers", "supplier_category_add": "suppliers",
    "supplier_category_edit": "suppliers", "supplier_category_delete": "suppliers",
    "procurement_list": "procurement", "procurement_add": "procurement",
    "procurement_detail": "procurement", "procurement_update_status": "procurement",
    "delivery_list": "deliveries", "delivery_detail": "deliveries",
    "delivery_receive": "deliveries",
    # Financials
    "transaction_list": "orders", "transaction_add": "orders",
    "transaction_detail": "orders", "transaction_set_status": "orders",
    "transaction_refund": "orders", "transaction_refund_approve": "orders",
    "transaction_refund_reject": "orders",
    "billing_list": "billing", "billing_create": "billing",
    "billing_detail": "billing", "billing_confirm": "billing",
    # System & Monitoring
    "reports_dashboard": "reports", "sales_report": "reports",
    "inventory_report": "reports", "procurement_report": "reports",
    "billing_report": "reports", "export_csv": "reports",
    "alerts_center": "alerts",
    "audit_log_list": "audit",
    "backup_status": "backups", "backup_run_now": "backups",
    "backup_record_restore_test": "backups",
    # Administration
    "branch_list": "branches", "branch_add": "branches",
    "branch_edit": "branches", "branch_delete": "branches",
    "user_list": "users", "user_add": "users",
    "user_edit": "users", "user_toggle_active": "users",
    "user_hard_delete": "users", "customer_list": "customers",
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


def storefront_categories(limit=None):
    """
    The flat list of categories the STOREFRONT is allowed to draw, the rule
    in one place. Every storefront surface renders this same list: the
    header's Shop dropdown, the home page's category tiles, the footer
    column, and the catalog's Category filter rail.

    Empty while the store has fewer than two categories a shopper could
    actually pick between.

    A lone category is not a choice. With one row in the table ("General",
    holding every product), the chip row offered a filter that filters
    nothing, the rail asked the shopper to pick between "All products" and
    the only category there is, and every card in the grid carried the same
    word. None of that told anyone anything; it just spent space saying
    "General" six times per screen. The moment a second category exists all
    of it comes back on its own — this is a threshold, not a deletion, and
    nothing about the category is removed from the database or the internal
    system.

    A function rather than a rule written twice: the catalog view used to
    pass its own unconditional ProductCategory.objects.all() for the filter
    rail, so on a one-category store the header hid its chips and the rail
    beside the grid still offered the single tick-box that filters nothing.

    `limit` caps the count for the fixed-width surfaces (the header's
    quick-link row takes 8). It must leave room for the threshold to be
    judged: a limit of 1 would slice the list to one category and then read
    that as "only one category exists".

    A category with nothing filed under it is not drawn at all. Every one of
    these surfaces is a link into a filtered catalog, and an empty category
    is a link to an empty grid — the storefront promising a shelf that isn't
    there. It is a threshold like the one above and not a deletion: the row
    stays in the database, stays in Inventory › Categories where the owner
    manages it, and reappears on the shop the moment a product is filed under
    it. This is also what keeps a placeholder like "General" — the category a
    catalog starts life with because Product needs one — out of the menu once
    its last product has been sorted into a real category.
    """
    from django.db.models import Count

    from inventory.models import ProductCategory

    if limit is not None and limit < 2:
        raise ValueError('limit must be at least 2, or the threshold cannot be judged')

    categories = list(
        ProductCategory.objects
        .annotate(product_total=Count('products', distinct=True))
        .filter(product_total__gt=0)
        .order_by('category_name')
    )

    # Counted BEFORE the limit is applied, so trimming a long category list
    # for a fixed-width surface can never itself trip the threshold.
    if len(categories) < 2:
        return []

    return categories[:limit] if limit else categories


def nav_categories(request):
    """
    Expose `nav_categories` for the storefront header's Shop dropdown and
    quick-link row — the flat category list.

    They sit in public_base.html, which cart, checkout, product detail and
    the order pages all extend — none of which were passing categories of their
    own, so the row would have rendered empty on most of the shop.

    The fewer-than-two rule, and why it exists, lives in
    storefront_categories() above.

    Costs nothing on internal pages: SimpleLazyObject defers the query until
    a template actually reads the variable, and no internal template names
    it, so it is never executed there.
    """
    from django.utils.functional import SimpleLazyObject

    return {"nav_categories": SimpleLazyObject(lambda: storefront_categories(limit=8))}


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

    return {"nav_branches": Branch.objects.filter(is_active=True)}


ALERT_COUNT_CACHE_SECONDS = 30


def alerts(request):
    """
    Expose `alert_count` for the sidebar/bottom-nav badge.

    Counts everything the Alerts Center page itself surfaces as needing
    attention — floor low stock, warehouse low stock, expired/expiring/
    approaching-expiry batches, and overdue deliveries (see
    inventory.alerts.urgent_alert_count) — so a badge of 0 means nothing on
    that page is waiting. Previously counted floor low stock only, which
    could read 0 while the page it links to had expired batches or overdue
    deliveries sitting on it (KATRINA-ALERT-001).

    Branch-scoped: a manager sees only their own branch's alert count.
    Kept cheap and fail-safe — a badge must never break a page render.

    Cached per-user for ALERT_COUNT_CACHE_SECONDS: this runs on every single
    internal page render (it's a context processor), but stock levels don't
    change every second, so re-running the alert queries on every nav click
    was pure latency with nothing to show for it. A stale badge for up to
    30s is a fine trade against paying several DB round trips per click.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    if getattr(user, "role", None) not in INTERNAL_ROLES:
        return {}

    from django.core.cache import cache

    cache_key = f"alert_count:{user.id}"
    # A cache backend error (Redis unreachable) must fail the same way a
    # miss does — this is the badge count, not the alert data itself, so
    # falling through to the real query below is the fail-safe path this
    # function's docstring already promises; only the ".get() worked, cache
    # is fine" branch was missing its own guard.
    try:
        count = cache.get(cache_key)
    except Exception:
        count = None
    if count is not None:
        return {"alert_count": count}

    try:
        from inventory.alerts import urgent_alert_count

        count = urgent_alert_count(branch_ids_for_user(user))
    except Exception:
        return {}

    try:
        cache.set(cache_key, count, ALERT_COUNT_CACHE_SECONDS)
    except Exception:
        pass
    return {"alert_count": count}


def internal_notifications(request):
    """
    Expose `owner_notification_count` for the topbar bell badge — see
    templates/internal_base.html and core.views.notification_center_view.

    Counts unresolved High/Critical items specifically (Notification.
    is_resolved), not raw unread count — matching the Notification Center's
    own "read ≠ resolved" rule: opening a card marks it read, only its
    action button resolves it, so the badge stays a count of what's still
    waiting on the Owner/Manager rather than emptying the moment they glance
    at the list. Same fail-safe, cached-count shape as alerts() above.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    if getattr(user, "role", None) not in INTERNAL_ROLES:
        return {}

    from django.core.cache import cache

    cache_key = f"owner_notification_count:{user.id}"
    count = cache.get(cache_key)
    if count is not None:
        return {"owner_notification_count": count}

    try:
        count = user.notifications.filter(
            requires_action=True, resolved_at__isnull=True
        ).count()
    except Exception:
        return {}

    cache.set(cache_key, count, ALERT_COUNT_CACHE_SECONDS)
    return {"owner_notification_count": count}


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

        branch = Branch.objects.filter(pk=branch_id, is_active=True).first()
        assignment = request.session.get('fulfillment_assignment') or {}
        distance = (
            assignment.get('distance_km')
            if branch and assignment.get('branch_id') == branch.pk else None
        )
        return {
            "nav_selected_branch": branch,
            "nav_branch_distance": distance,
        }
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

    Fail-safe on both counts — a badge must never break a page render.
    """
    out = {}

    session = getattr(request, "session", None)
    if session is not None:
        try:
            cart = session.get("cart", {})
            # Only count a cart that has a branch behind it. A line can only be
            # added once a branch is chosen (add_to_cart), and nothing clears
            # the branch without clearing the cart, so "cart set, no branch" is
            # a stale artifact of an older build — the cart page will refuse to
            # show it and send the shopper to pick a branch, so the badge must
            # not advertise a count for it either.
            out["cart_count"] = len(cart) if session.get("branch_id") else 0
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
