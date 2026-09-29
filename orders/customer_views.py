import math
import time
import uuid
from decimal import Decimal
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.functional import SimpleLazyObject
from django.utils.http import url_has_allowed_host_and_scheme

from branches.models import Branch
from core.context_processors import storefront_categories
from inventory.alerts import stock_status
from inventory.models import Inventory, Product, ProductCategory
from inventory.stats import (
    attach_storefront_counts, in_stock_product_ids, units_sold_by_product,
)
from sales.models import SalesItem, SalesTransaction
from sales.pricing import price_cart
from sales.pwd_requests import (
    new_request_fields, notify_pwd_request_submitted, request_can_reach_cart,
    validate_pwd_details,
)
from .services import (
    customer_fulfillment_location,
    fulfillable_quantities,
    remember_assignment,
    resolve_nearest_branch,
)

# Cap on how many rows the live-search dropdown shows at once.
SEARCH_SUGGESTION_LIMIT = 8

# Free delivery threshold shown on the cart meter.
FREE_DELIVERY_OVER = Decimal('500')

# Shown when a shopper reaches the cart or checkout with items but no branch
# set. The cart is priced and stocked per branch, so neither page can mean
# anything until one is chosen. add_to_cart already requires a branch before
# a line can be added, and nothing clears the branch without also clearing
# the cart, so in practice this is only reachable by a session that predates
# that guarantee.
PICK_BRANCH_TO_CHECK_OUT = 'Pick a branch to check out.'

NO_SINGLE_BRANCH = (
    'No single branch can fulfill every item in your cart right now. '
    'We will not split one order across branches; reduce a quantity or remove '
    'an item and try again.'
)

# Catalog sort orders. The dict is the single source of truth for both the
# <select> the template renders and the validation of ?sort= — an unknown or
# hand-edited value falls back to DEFAULT_SORT rather than 500ing on a
# missing key.
#
# "Popular" is the reference's "Featured" slot filled with a real figure —
# units actually sold — rather than a curation flag no one in this system can
# set. There is no featured/pinned column on Product, so a literal "Featured"
# order would just be an arbitrary one wearing an editorial label.
SORT_OPTIONS = {
    'popular': {
        'label': 'Popular',
        'key': lambda r: (-r['product'].units_sold, r['product'].product_name),
    },
    'price_asc': {
        'label': 'Price: Low to High',
        'key': lambda r: (r['product'].unit_price, r['product'].product_name),
    },
    'price_desc': {
        'label': 'Price: High to Low',
        'key': lambda r: (-r['product'].unit_price, r['product'].product_name),
    },
    'name': {
        'label': 'Name A–Z',
        'key': lambda r: r['product'].product_name.lower(),
    },
}
DEFAULT_SORT = 'popular'


def is_customer(user):
    return user.is_authenticated and user.role == 'CUSTOMER'


def customer_required(view):
    """Gate the cart and checkout on a CUSTOMER account, without looping.

    @customer_required sent BOTH kinds of refusal to the login
    page, and for a signed-in staff or owner account that is an infinite
    redirect: login_view honours ?next= for a user who is already
    authenticated, so it hands them straight back to the view that just
    refused them, which sends them to login again. The browser gives up with
    ERR_TOO_MANY_REDIRECTS; from the shopper's side, "Add to cart" simply
    does nothing. It is reproducible in three lines with the test client,
    which raises RedirectCycleError.

    The two refusals are not the same thing and no longer get the same
    answer. Someone with no session is asked to sign in, with their
    destination preserved so the button they pressed still happens
    afterwards. Someone signed in to a non-customer account is TOLD why —
    the storefront cart belongs to customer accounts; staff sell through the
    POS — and left on the catalog, which they can still read.

    The AJAX branch matters because add_to_cart has one: catalog-live.js
    expects JSON, and a 302 to an HTML page would surface as a silent
    failure rather than a message.
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

        if not user.is_authenticated:
            if is_ajax:
                return JsonResponse(
                    {'ok': False, 'error': 'Please sign in to continue.'}, status=401
                )
            return redirect(f"{reverse('accounts:login')}?next={quote(request.get_full_path())}")

        if not is_customer(user):
            error = (
                'The shop cart is for customer accounts. '
                'Staff sales go through the POS instead.'
            )
            if is_ajax:
                return JsonResponse({'ok': False, 'error': error}, status=403)
            messages.error(request, error)
            return redirect('orders:catalog')

        return view(request, *args, **kwargs)

    return _wrapped


def _safe_next(request):
    """The POSTed `next`, but only if it points back into this site.

    `next` is attacker-controllable, and redirect() will happily send the
    browser to another origin, so it is validated here rather than trusted.
    Returns None when absent or off-site, and the caller falls back to its
    own default.
    """
    nxt = request.POST.get('next')
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return None


def _safe_get_next(request):
    """GET equivalent of ``_safe_next`` for the confirmation page link."""
    nxt = request.GET.get('next')
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return None


def _safe_referer(request):
    """The Referer header, but only if it points back into this site.

    The same reasoning as _safe_next, for the one caller that has no `next`
    field to read. Referer arrives from the browser, but whoever authored the
    page that linked here chose what it says, and redirect() will follow it
    off-origin without complaint. Returns None when absent or off-site, and
    the caller falls back to its own default.
    """
    ref = request.META.get('HTTP_REFERER')
    if ref and url_has_allowed_host_and_scheme(
        ref, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return ref
    return None


def _resolve_cart_branch(request, cart=None, *, notify_change=False):
    cart = request.session.get('cart', {}) if cart is None else cart
    previous_id = request.session.get('branch_id')
    location = customer_fulfillment_location(request.user, request.session)
    resolution = resolve_nearest_branch(
        location['latitude'],
        location['longitude'],
        cart,
        city=location['city'],
        barangay=location['barangay'],
        preferred_branch_id=previous_id,
    )
    if resolution.branch:
        remember_assignment(request.session, resolution)
        if notify_change and previous_id and previous_id != resolution.branch.pk:
            messages.info(
                request,
                f'Your fulfillment branch changed to {resolution.branch.branch_name}. '
                'Every cart item was rechecked there.',
            )
    return resolution


def _assignment_distance(request, branch):
    assignment = request.session.get('fulfillment_assignment') or {}
    if branch and assignment.get('branch_id') == branch.pk:
        return assignment.get('distance_km')
    return None


def _cart_items(request):
    """Resolve the session cart into rows + total. Drops stale entries.

    Priced at the tier each line's own quantity earns — the same one checkout()
    will use — so the figures here are what they will actually be billed. The
    session cart holds one entry per product, so the quantity read here is
    already the shopper's whole commitment to that item. The PWD/Senior
    discount is deliberately NOT previewed, and since F-04 it cannot be
    claimed here at all: it is granted by staff, against a sighted ID, after
    the order exists. Showing it on the cart would promise a price the order
    is not entitled to and may never get.

    Returns (items, total, dropped_names). S-1 — a line that can no longer be
    priced at all (delisted, the branch stopped carrying it, its last lot
    expired since it was added) used to just vanish from this list with no
    trace, while staying in request.session['cart'] — so the cart page's
    total quietly shrank, the header badge (which counts the raw session
    cart) kept the old number, and checkout met the same line for the first
    time, rejecting a product the shopper could no longer even see on the
    page in front of them. It is popped out of the session HERE, the one
    place every caller of this helper shares, so the page, the badge and
    checkout can never disagree about what happened to it again. A line that
    still exists but is merely short (`short` below) is NOT dropped — that is
    a normal, visible state the cart page already shows a warning for.
    """
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})
    # No branch → nothing to price the cart against. view_cart and checkout
    # now redirect to branch selection before they get here, so this is a
    # belt-and-braces guard for any future caller: it keeps this helper from
    # quietly returning an empty cart that disagrees with the header badge.
    if not branch_id:
        return [], Decimal('0'), []
    items, total, dropped = [], Decimal('0'), []
    changed = False
    available_by_product = {
        product_id: quantity
        for (resolved_branch_id, product_id), quantity in fulfillable_quantities(
            [branch_id], cart.keys()
        ).items()
        if resolved_branch_id == branch_id
    }

    for product_id, qty in list(cart.items()):
        inv = Inventory.objects.sellable().select_related('product').prefetch_related(
            'product__price_tiers', 'product__images'
        ).filter(
            product_id=product_id, branch_id=branch_id
        ).first()
        if not inv:
            # product_id could not be resolved to a real Inventory row at all
            # (the product, or its row at this branch, was deleted after it
            # was added) — as opposed to a row that exists and merely reads 0
            # available. Purged from the session cart below, not just
            # skipped from `items`: a line that only vanishes from the
            # display still sits in request.session['cart'], which
            # checkout() reads directly and would otherwise refuse forever
            # with "0 left" for an item the shopper can no longer even see
            # to remove.
            product = Product.objects.filter(pk=product_id).first()
            dropped.append(product.product_name if product else 'An item')
            cart.pop(product_id, None)
            changed = True
            continue
        available_qty = available_by_product.get(inv.product_id, 0)
        unit_price = inv.product.price_at(qty)
        subtotal = unit_price * qty
        total += subtotal
        items.append({
            'product': inv.product,
            'quantity': qty,
            # The tier price, not product.unit_price — the template shows this
            # as the "each" figure, so it must not read the retail price back
            # off the product and undo the tier.
            'unit_price': unit_price,
            'subtotal': subtotal,
            'in_stock': available_qty,
            'short': qty > available_qty,
            # Same tiering the Floor/Warehouse Stock lists use (S-4) — no
            # more hardcoded "<= 10" in the template disagreeing with what
            # Inventory's own reorder_level says is actually low.
            'stock_status': stock_status(available_qty, inv.reorder_level),
        })

    if changed:
        request.session['cart'] = cart
    return items, total, dropped


# ─────────────────────────────────────────────
# BROWSING — open to guests (log in only to buy)
# ─────────────────────────────────────────────

def _catalog_stock(branch_filters, search_query):
    """Return the branch/search-filtered inventory shared by results and facets.

    Category is intentionally absent here: category facet counts describe
    what each category would contain under the current branch/search scope,
    not only the categories that are already selected.
    """
    stock = Inventory.objects.sellable().filter(branch__is_active=True)
    if branch_filters:
        stock = stock.filter(branch_id__in=branch_filters)
    if search_query:
        stock = stock.filter(product__product_name__icontains=search_query)
    return stock


def product_catalog(request):
    branches = Branch.objects.filter(is_active=True)

    # The Filters rail's Branch checklist is a BROWSE filter and nothing more:
    # multi-select, exactly like the Category checklist beside it. Tick any
    # combination and the grid lists what those branches have on the shelf; it
    # does not touch request.session['branch_id'] or the cart.
    #
    # The branch the cart belongs to — the "shopping branch" — is a separate
    # thing, set only by the explicit controls (the nav bar's "Change" link,
    # the select_branch page, a card's "Choose a branch" button), all of which
    # route through _start_shopping_at(). The two used to be fused here: a
    # single tick called _start_shopping_at(), so glancing at another branch's
    # shelves silently emptied the cart — and on the live-filter (XHR) path the
    # explanatory message was never even rendered.
    #
    # `branch` is only read as a filter when it is actually in the query
    # string. A shopper who chose a branch and then clicked Shop in the nav
    # arrives with no parameters at all, and the grid should still show their
    # branch's stock — so that case falls back to the session branch.
    branch_id = request.session.get('branch_id')
    if 'branch' in request.GET:
        wanted = [b for b in request.GET.getlist('branch') if b.isdigit()]
        # Round-tripped through the database rather than trusted: an id that
        # names no branch would otherwise sit in the URL, tick nothing in the
        # rail, and quietly filter the grid down to zero rows.
        valid = list(branches.filter(pk__in=wanted).values_list('pk', flat=True))
        branch_filters = [str(pk) for pk in valid]
    else:
        branch_filters = [str(branch_id)] if branch_id else []

    # The single branch whose shelves the grid is currently showing, for the
    # summary's "N of M items at <branch>" line. It follows the Branch
    # checklist above (a browse filter), which since the decoupling is no
    # longer the same thing as the session's shopping branch — so this is
    # deliberately NOT selected_branch_id.
    summary_branch = None
    if len(branch_filters) == 1:
        summary_branch = branches.filter(pk=branch_filters[0]).first()

    # sellable() annotates available_qty — the non-expired portion of each
    # row's stock — so a product with an expired lot at one branch and a
    # fresh one at another (or even a fresh AND an expired lot in the same
    # row) is never hidden entirely, just correctly sized.
    search_query = request.GET.get('q', '')
    stock = _catalog_stock(branch_filters, search_query).select_related(
        'product', 'product__category'
    ).prefetch_related('product__images')

    # getlist, not get: the rail is a checklist now, so "Rice AND Canned
    # goods" is a question a shopper can ask. Non-numeric values are dropped
    # rather than passed to the ORM, which raises on a bad pk instead of
    # simply matching nothing.
    category_filters = [c for c in request.GET.getlist('category') if c.isdigit()]
    if category_filters:
        stock = stock.filter(product__category_id__in=category_filters)

    # Collapse to one row per product when no branch is chosen; keep the
    # best availability so stock signals stay honest.
    rows = {}
    for inv in stock:
        key = inv.product_id
        if key not in rows or inv.available_qty > rows[key]['qty']:
            rows[key] = {'product': inv.product, 'qty': inv.available_qty}

    # The storefront does not list what it cannot sell. Filtered AFTER the
    # collapse above, not before it: with no branch chosen a product can be
    # empty at one branch and stocked at another, and dropping the empty row
    # early would have hidden a product that is genuinely available.
    rows = {pid: row for pid, row in rows.items() if row['qty'] > 0}

    ordered_rows = list(rows.values())

    # The card shows a "N sold" line where the design reference shows a star
    # rating — there is no review model, and this is a real figure. One
    # aggregate query for the whole page, not one per card.
    sold = units_sold_by_product([r['product'].id for r in ordered_rows])
    for row in ordered_rows:
        row['product'].units_sold = sold.get(row['product'].id, 0)

    # Sorted in Python, not in the queryset: `rows` above already collapsed
    # multiple branch Inventory rows down to one entry per product, and
    # units_sold was only just attached, so there is no queryset left to
    # order by either of them. The list is one page of products, not a table
    # scan, so the cost is nil.
    sort = request.GET.get('sort', '')
    if sort not in SORT_OPTIONS:
        sort = DEFAULT_SORT
    ordered_rows.sort(key=SORT_OPTIONS[sort]['key'])

    # Memoised, because both `active_categories` (the breadcrumb) and
    # `active_chips` need it and neither should pay for the other's query.
    # A list rather than a queryset for the same reason: a queryset re-runs
    # on each iteration, and the breadcrumb tests its length before reading
    # its first row.
    _resolved = []

    def _active_categories():
        if not _resolved:
            _resolved.append(list(
                ProductCategory.objects.filter(pk__in=category_filters)
                .order_by('category_name')
            ))
        return _resolved[0]

    context = {
        'rows': ordered_rows,
        'branches': branches,
        'selected_branch_id': branch_id,
        # Not ProductCategory.objects.all(): the whole storefront hides its
        # category UI below two categories (core.context_processors.
        # storefront_categories), and this rail was the one surface still
        # ignoring that — on a one-category store the header dropped its
        # chips while the rail beside the grid kept offering the single
        # tick-box that filters nothing.
        #
        # Lazy so callers that inspect only result rows do not pay for the
        # tree. Live responses deliberately render the category partial from
        # this object, using the branch ids carried by that exact request.
        #
        # Each category carrying a live `stock_count`. See _category_rail.
        'categories': SimpleLazyObject(
            lambda: _category_rail(
                branch_filters, search_query, category_filters
            )
        ),
        'search_query': search_query,
        'category_filters': category_filters,
        # The ticked categories themselves, for the removable chips above the
        # grid and the breadcrumb's trail. Ids alone cannot name themselves,
        # and the rail's tree has empty rows dropped from it, so a chip read
        # off that would vanish for a category that is genuinely ticked and
        # genuinely has nothing in stock — leaving a filtered grid with no
        # visible reason and no way back out.
        'active_categories': SimpleLazyObject(_active_categories),
        # Reads active_categories through the same lazy callable rather than
        # off the context, so neither list is queried on the live-search XHR
        # path — which renders neither partial that names them.
        'active_chips': SimpleLazyObject(
            lambda: _active_chips(
                search_query, _active_categories(), branches, branch_filters
            )
        ),
        'branch_filters': branch_filters,
        'summary_branch': summary_branch,
        'sort': sort,
        # Everything except `sort`, pre-encoded, so the sort links can carry
        # the live filters without the template having to loop the category
        # list into a query string by hand — which it cannot do correctly now
        # that there can be several of them.
        'filter_qs': _filter_querystring(search_query, category_filters, branch_filters),
        # The unfiltered figure behind "N of M items". S-6 — this used to be
        # Product.objects.count(), every row in the product master including
        # ones no branch has ever stocked and ones out everywhere, so a shop
        # with twelve buyable things read "12 of 340" and looked like the
        # filters had hidden 96% of the store. Counted the same way the grid
        # itself is populated — distinct products with sellable stock
        # somewhere — so it does not move when a category or a branch is
        # picked, and "12 of 12" / "3 of 12" are both honest readings.
        'total_count': Inventory.objects.sellable().filter(
            available_qty__gt=0
        ).values('product_id').distinct().count(),
        'sort_options': [
            {'value': value, 'label': option['label']}
            for value, option in SORT_OPTIONS.items()
        ],
    }

    # Live search (catalog-live.js) asks for just the two regions that
    # change — avoids re-fetching/re-rendering the whole page (branch tabs,
    # category list, chrome) on every keystroke.
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'summary_html': render_to_string('partials/catalog_result_summary.html', context, request=request),
            'grid_html': render_to_string('partials/catalog_grid.html', context, request=request),
            'category_html': render_to_string('partials/catalog_category_filter.html', context, request=request),
        })

    return render(request, 'orders/catalog.html', context)


def search_suggestions(request):
    """JSON endpoint backing the header's live-search dropdown."""
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})

    # Suggestions lead straight to a product page, so they pass the same
    # in-stock gate as the listings — otherwise the one place a shopper can
    # still reach an unavailable product is the search box.
    products = (
        Product.objects.select_related('category')
        .filter(Q(product_name__icontains=query) | Q(sku_code__icontains=query))
        .filter(id__in=in_stock_product_ids(request.session.get('branch_id')))
        .order_by('product_name')[:SEARCH_SUGGESTION_LIMIT]
    )

    results = [
        {
            'id': product.id,
            'name': product.product_name,
            'category': product.category.category_name if product.category else '',
            'price': str(product.unit_price),
            'url': reverse('orders:product_detail', args=[product.id]),
        }
        for product in products
    ]
    return JsonResponse({'results': results})


def product_detail(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    branch_id = request.session.get('branch_id')
    # Same treatment the grid cards get, so the detail page can show the
    # "In stock · N sold" line instead of making the shopper infer both from
    # the per-branch table below it.
    #
    # Scoped to the chosen branch, because product.on_hand does two jobs in
    # this template and only the branch figure is right for either. It draws
    # the stock chip AND it gates the Add to Cart button — and the cart only
    # ever accepts stock from the session's branch (add_to_cart looks up one
    # Inventory row, for this product at this branch). Summed across every
    # branch it told a shopper at Tarcan "In stock" for something only
    # Makinabang has, offered them the button, and let add_to_cart refuse it.
    # With no branch chosen yet the all-branch sum is still the honest
    # reading — "available somewhere in the store" — and the panel is showing
    # "Choose a branch to buy" rather than a live button.
    #
    # The per-branch table underneath is unaffected: it reads its own
    # Inventory rows and still breaks the whole store down, branch by branch.
    attach_storefront_counts([product], branch_id=branch_id)
    branches = Branch.objects.filter(is_active=True)

    branch_stock = []
    for branch in branches:
        inv = Inventory.objects.sellable().filter(product=product, branch=branch).first()
        branch_stock.append({
            'branch': branch,
            'available': bool(inv and inv.available_qty > 0),
            'qty': inv.available_qty if inv else 0,
        })

    # "Recently viewed" rail. A short list of product ids kept in the session,
    # most-recent-first, with this product pushed to the head on every visit.
    # It is a browsing aid, not a security boundary, so it lives in the
    # session like the cart does and is never persisted.
    RECENTLY_VIEWED_MAX = 12
    already_seen = [
        pid for pid in request.session.get('recently_viewed', []) if pid != product.id
    ]
    request.session['recently_viewed'] = (
        [product.id] + already_seen[:RECENTLY_VIEWED_MAX - 1]
    )

    # The gate every storefront listing passes through — one grouped scan of
    # Inventory, shared here by both rails below.
    in_stock_ids = in_stock_product_ids(branch_id)

    # Built from what was seen BEFORE this visit — the product on screen has
    # no place in its own "recently viewed" rail. Ordered newest-first, and
    # filtered to what is still buyable: the same gate the "You may also
    # like" rail uses, for the same reason.
    recent_ids = already_seen[:6]
    recent_by_id = {
        p.id: p
        for p in attach_storefront_counts(
            list(
                Product.objects.filter(id__in=recent_ids)
                .filter(id__in=in_stock_ids)
                .select_related('category')
                .prefetch_related('images')
            ),
            branch_id=branch_id,
        )
    } if recent_ids else {}
    recently_viewed = [recent_by_id[pid] for pid in recent_ids if pid in recent_by_id]

    # Whether the buy-more-save-more ladder gets a working quantity input to
    # sync against. The same conditions that reach the live Add to Cart form
    # further down the template (logged in, a Customer account, a branch
    # chosen, stock at it) — computed once here instead of repeated in the
    # template so the ladder and the form can never end up disagreeing about
    # which case they are in.
    can_buy = bool(
        request.user.is_authenticated
        and request.user.role == 'CUSTOMER'
        and product.on_hand > 0
    )

    return render(request, 'orders/product_detail.html', {
        'product': product,
        'branch_stock': branch_stock,
        'can_buy': can_buy,
        # attach_storefront_counts so the related cards can offer the same
        # Add-to-cart CTA as the catalog and home grids instead of a "View"
        # link back into another product page. Scoped to the chosen branch
        # when there is one, so the stock line matches what the cart will
        # actually accept.
        # Only what is actually buyable — a "You might also like" rail is a
        # recommendation, and recommending something out of stock wastes the
        # click. Same gate the catalog and home grids use.
        'related': attach_storefront_counts(
            list(
                Product.objects.filter(category=product.category)
                .exclude(id=product.id)
                .filter(id__in=in_stock_ids)
                .select_related('category')
                .prefetch_related('images')[:6]
            ),
            branch_id=branch_id,
        ),
        'recently_viewed': recently_viewed,
        'selected_branch_id': branch_id,
        # The Branch object, not just its id: the stock chip and the disabled
        # Add to Cart button both name the branch they are talking about, and
        # `branches` above is the whole list, not the chosen one.
        'selected_branch': next((b for b in branches if b.pk == branch_id), None),
        # TODO: Dropped is_wholesale check — order_type_for() was removed in
        # bb3aa4d (tier-pricing migration). Needs Marie's input on what should
        # replace this for the BULK badge gating logic.
    })


def _category_rail(branch_filters, search_query, category_filters=()):
    """The Filters rail's Category list, with a live in-stock count per row.

    The counts are the reference's "Biscuits (348)", and they are counted the
    same way the grid is: sellable stock only, narrowed by whatever branches
    and search term are in play, so a row reading (0) genuinely has nothing
    on it here rather than nothing at this branch while the number claims
    otherwise. What they are deliberately NOT narrowed by is the category
    filter itself — a count that collapsed to the ticked category would
    print (6) beside it and (0) beside every other row, which says nothing
    about what ticking those rows would do.

    Rows with nothing behind them are dropped. An empty row in a filter rail
    is a promise the grid then breaks.

    One aggregate query on top of storefront_categories()'s own, and only on
    the full-page path — product_catalog wraps this in a SimpleLazyObject,
    and the live-search XHR renders neither partial that names it.
    """
    categories = storefront_categories()
    if not categories:
        return []

    stock = _catalog_stock(branch_filters, search_query)
    selected = {str(category_id) for category_id in category_filters}

    # distinct() because a product stocked at three branches is three
    # Inventory rows and one product on the shelf.
    counts = {}
    pairs = stock.filter(
        available_qty__gt=0, product__category__isnull=False
    ).values_list('product__category_id', 'product_id').distinct()
    for category_id, product_id in pairs:
        counts.setdefault(category_id, set()).add(product_id)

    rail = []
    for category in categories:
        count = len(counts.get(category.id, ()))
        if not count:
            continue
        category.stock_count = count
        rail.append(category)
    return rail


def _filter_querystring(search_query, category_filters, branch_filters):
    """`q` and every ticked category and branch, urlencoded, without `sort`."""
    params = QueryDict(mutable=True)
    if search_query:
        params['q'] = search_query
    if category_filters:
        params.setlist('category', category_filters)
    if branch_filters:
        params.setlist('branch', branch_filters)
    return params.urlencode()


def _active_chips(search_query, active_categories, branches, branch_filters):
    """The removable "Category: Fresh Beef ×" chips at the top of the rail.

    Each chip carries `remaining_qs` — this exact filter set with that one
    item taken out — so the × is a plain link that works with JavaScript off.
    Built here rather than in the template because dropping one value out of
    a multi-valued query string is not something Django's template language
    can express, and doing it by string surgery in HTML is how a `category=`
    ends up half-removed.

    `branch` is always written into the remaining set, even when removing the
    chip empties it. Leaving the key out entirely means something different
    to product_catalog — "no branch filter in this request", which falls back
    to the session's shopping branch — so a shopper clearing their last
    branch chip would watch the grid keep that branch anyway. Same reason the
    rail's Branch fieldset carries a hidden empty input.
    """
    chips = []
    branch_by_id = {str(b.pk): b for b in branches}

    def remaining(q=None, categories=None, wanted_branches=None):
        params = QueryDict(mutable=True)
        if q:
            params['q'] = q
        if categories:
            params.setlist('category', categories)
        params.setlist('branch', wanted_branches or [''])
        return params.urlencode()

    category_ids = [str(c.pk) for c in active_categories]

    if search_query:
        chips.append({
            'label': 'Search',
            'value': search_query,
            'remaining_qs': remaining(categories=category_ids, wanted_branches=branch_filters),
        })

    for category in active_categories:
        value = category.category_name
        chips.append({
            'label': 'Category',
            'value': value,
            'remaining_qs': remaining(
                q=search_query,
                categories=[c for c in category_ids if c != str(category.pk)],
                wanted_branches=branch_filters,
            ),
        })

    for branch_id in branch_filters:
        branch = branch_by_id.get(branch_id)
        if branch is None:
            continue
        chips.append({
            'label': 'Branch',
            'value': branch.branch_name,
            'remaining_qs': remaining(
                q=search_query,
                categories=category_ids,
                wanted_branches=[b for b in branch_filters if b != branch_id],
            ),
        })

    return chips


def select_branch(request):
    """Consent screen for location-assisted automatic branch assignment.

    A posted ``branch_id`` is intentionally ignored. The server always
    resolves one branch for the complete cart.
    """
    if request.method == 'POST':
        status = request.POST.get('location_status', '')
        if status == 'granted':
            from accounts.geocoding import GeocodingError, parse_coordinates, reverse_geocode

            try:
                latitude, longitude = parse_coordinates(
                    request.POST.get('latitude'), request.POST.get('longitude')
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect('orders:select_branch')
            location_payload = {
                'latitude': str(latitude),
                'longitude': str(longitude),
                'captured_at': time.time(),
            }
            try:
                accuracy = float(request.POST.get('accuracy', ''))
            except (TypeError, ValueError):
                accuracy = None
            if accuracy is not None and math.isfinite(accuracy) and 0 <= accuracy <= 100000:
                location_payload['accuracy_m'] = round(accuracy)
            label = None
            if settings.GEOCODING_REVERSE_URL:
                try:
                    label = reverse_geocode(latitude, longitude)
                except (GeocodingError, ValueError):
                    pass
            if label:
                location_payload['label'] = label
            request.session['fulfillment_location'] = location_payload
        else:
            request.session.pop('fulfillment_location', None)

        resolution = _resolve_cart_branch(request, notify_change=True)
        location = customer_fulfillment_location(request.user, request.session)
        if not resolution.branch:
            messages.error(request, NO_SINGLE_BRANCH)
        elif status == 'granted':
            messages.success(
                request,
                f'Location detected. {resolution.branch.branch_name} can fulfill '
                'your whole cart.',
            )
        else:
            fallback_description = (
                f'your saved address ({location["label"]})'
                if location['source'] == 'address' and location['label']
                else 'your saved address'
                if location['source'] == 'address'
                else 'the configured store fallback'
            )
            messages.info(request, f'Location was not shared. Using {fallback_description}.')

        # Return to this page so the shopper can see which location was used
        # and which branch it selected before continuing.
        destination = _safe_next(request) or reverse('orders:cart')
        confirmation_url = reverse('orders:select_branch')
        return redirect(f'{confirmation_url}?next={quote(destination, safe="")}')

    current_branch = Branch.objects.filter(
        pk=request.session.get('branch_id'), is_active=True
    ).first()
    location = customer_fulfillment_location(request.user, request.session)
    continue_url = _safe_get_next(request) or reverse('orders:cart')
    return render(request, 'orders/select_branch.html', {
        'current_branch': current_branch,
        'assignment_distance': _assignment_distance(request, current_branch),
        'assignment_strategy': (
            (request.session.get('fulfillment_assignment') or {}).get('strategy', '')
        ),
        'location_used': location,
        'continue_url': continue_url,
        'next': continue_url,
    })


# ─────────────────────────────────────────────
# CART + CHECKOUT — customers only
# ─────────────────────────────────────────────

@customer_required
def add_to_cart(request, product_id):
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    back = _safe_next(request)

    # The quantity is raw POST input. Two failure modes, handled here before
    # it can reach the stock comparison below:
    #   - non-numeric ("abc") used to raise ValueError out of int() and 500.
    #     Fall back to 1, the same default the field carries when omitted.
    #   - a negative (or zero) value parsed fine but then sailed through the
    #     `already + qty > available` guard trivially and *shrank* the cart
    #     line instead of erroring. Reject it at the cast site.
    try:
        qty = int(request.POST.get('quantity', 1) or 1)
    except (TypeError, ValueError):
        qty = 1
    if qty < 1:
        error = 'Enter a quantity of at least 1.'
        if is_ajax:
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(back or 'orders:catalog')

    product = get_object_or_404(Product, pk=product_id)
    cart = request.session.get('cart', {})
    already = cart.get(str(product_id), 0)
    proposed_cart = dict(cart)
    proposed_cart[str(product_id)] = already + qty
    resolution = _resolve_cart_branch(
        request, proposed_cart, notify_change=True
    )
    if not resolution.branch:
        error = NO_SINGLE_BRANCH
        if is_ajax:
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(back or 'orders:catalog')

    branch_id = resolution.branch.pk
    inv = Inventory.objects.sellable().filter(
        product_id=product_id, branch_id=branch_id
    ).select_related('product', 'branch').first()
    if inv is None:
        error = f'{product.product_name} is not currently available for fulfillment.'
        if is_ajax:
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(back or 'orders:catalog')

    available = fulfillable_quantities([branch_id], [product_id]).get(
        (branch_id, product_id), 0
    )
    if already + qty > available:
        if available == 0:
            error = f'{inv.product.product_name} is out of stock for fulfillment.'
        else:
            error = f'Only {available} of {inv.product.product_name} left for fulfillment.'
        if is_ajax:
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(back or 'orders:catalog')

    request.session['cart'] = proposed_cart
    # Remember exactly what we just added so the toast's "Undo" can reverse it.
    request.session['last_added'] = {'id': str(product_id), 'qty': qty}
    success = f'Added {inv.product.product_name} to your cart.'
    if is_ajax:
        # No redirect — the storefront JS confirms the add on the button
        # itself, so the customer keeps their scroll position instead of
        # jumping to the top of a re-rendered page.
        #
        # No undo_url/cart_url either: those fed the success toast's two
        # buttons, and there is no success toast any more. The Undo path
        # still exists for the message rendered on the no-JavaScript POST
        # below, which carries its own URL from the template.
        return JsonResponse({
            'ok': True,
            'message': success,
            'cart_count': len(proposed_cart),
        })
    messages.success(request, success, extra_tags='undoable')
    return redirect(back or 'orders:catalog')


@customer_required
def undo_add(request):
    """Reverse the most recent Add to Cart — backs the toast's Undo button.

    Called by fetch() from toast.js, so the happy path answers in JSON and the
    toast dismisses itself with no reload. Falls back to a plain redirect (with
    a confirmation message) when JavaScript is off or the fetch fails.
    """
    if request.method != 'POST':
        return redirect('orders:catalog')

    last = request.session.pop('last_added', None)
    cart = request.session.get('cart', {})
    if last and last['id'] in cart:
        cart[last['id']] -= last['qty']
        if cart[last['id']] <= 0:
            cart.pop(last['id'])
        request.session['cart'] = cart

    cart_count = len(cart)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'cart_count': cart_count})

    messages.info(request, 'Removed from your cart.')
    # Every other redirect in this file validates where it is sending the
    # browser; this one used to hand redirect() the raw Referer header, which
    # is chosen by whoever built the page that linked here and can therefore
    # name another origin. Undo is a bare <button> driven by toast.js fetch(),
    # with no form and so no `next` field — _safe_next is here for a future
    # caller that grows one, and the validated Referer is what actually
    # answers on this path.
    return redirect(
        _safe_next(request) or _safe_referer(request) or 'orders:catalog'
    )


@customer_required
def update_cart(request, product_id):
    """Bump one line up or down. Floors at 1 and never exceeds branch stock."""
    if request.method != 'POST':
        return redirect('orders:cart')

    cart = request.session.get('cart', {})
    key = str(product_id)
    if key not in cart:
        messages.info(request, 'That item is no longer in your cart.')
        return redirect('orders:cart')

    # Every branch below says what it did or why it declined. The refusals
    # already spoke; the successes and the floor did not, and a + or − that
    # changes a number without a word is the same to read as one that failed
    # to register — especially the floor, where nothing on screen moves.
    product = Product.objects.filter(pk=product_id).first()
    if not product:
        cart.pop(key, None)
        request.session['cart'] = cart
        _resolve_cart_branch(request, cart, notify_change=True)
        messages.error(request, 'That item is no longer available, so it came out of your cart.')
        return redirect('orders:cart')
    name = product.product_name
    action = request.POST.get('action')
    if action == 'increase':
        proposed_cart = dict(cart)
        proposed_cart[key] += 1
        resolution = _resolve_cart_branch(request, proposed_cart, notify_change=True)
        if not resolution.branch:
            current_branch_id = request.session.get('branch_id')
            if current_branch_id:
                available = fulfillable_quantities(
                    [current_branch_id], [product_id]
                ).get((current_branch_id, product_id), 0)
                messages.error(
                    request, f'Only {available} of {name} left at this branch.'
                )
            messages.error(request, NO_SINGLE_BRANCH)
            return redirect('orders:cart')
        cart = proposed_cart
        messages.success(request, f'{name} — {cart[key]} in your cart.')
    elif action == 'decrease':
        # Minimum of 1 — Remove is what clears the line entirely, and at the
        # floor that has to be said rather than silently doing nothing.
        if cart[key] <= 1:
            messages.info(
                request,
                f'{name} is down to one. Use Remove to take it out of your cart.'
            )
        else:
            cart[key] -= 1
            _resolve_cart_branch(request, cart, notify_change=True)
            messages.success(request, f'{name} — {cart[key]} in your cart.')
    else:
        # Neither button — a malformed or tampered POST. Nothing changed, so
        # this exit says so instead of redirecting to an unchanged cart in
        # silence, which would read as the press having been swallowed.
        messages.error(request, 'That cart change was not understood.')

    request.session['cart'] = cart
    return redirect('orders:cart')


@customer_required
def remove_from_cart(request, product_id):
    """Drop a line from the cart completely."""
    if request.method != 'POST':
        return redirect('orders:cart')

    cart = request.session.get('cart', {})
    if cart.pop(str(product_id), None) is not None:
        request.session['cart'] = cart
        _resolve_cart_branch(request, cart, notify_change=True)
        messages.info(request, 'Removed from your cart.')
    else:
        # S-9 — a second tab, a stale page, a double-click: the line was
        # already gone. This used to reload the cart in silence, which reads
        # exactly like the Remove button not working — the one thing every
        # other exit on this page already says something about.
        messages.info(request, 'That item was already removed from your cart.')
    return redirect('orders:cart')


@customer_required
def view_cart(request):
    # A cart with lines in it but no branch cannot be priced or checked out —
    # stock and prices are per-branch. Rather than render a cart that shows
    # nothing while the header badge shows a count, send the shopper to pick a
    # branch. (_start_shopping_at then clears the stale lines, so this also
    # cleans up a session left in that state by an older build.)
    if request.session.get('cart') and not request.session.get('branch_id'):
        messages.error(request, PICK_BRANCH_TO_CHECK_OUT)
        return redirect('orders:select_branch')

    resolution = _resolve_cart_branch(request, notify_change=True)
    if request.session.get('cart') and not resolution.branch:
        messages.error(request, NO_SINGLE_BRANCH)
    items, total, dropped_items = _cart_items(request)
    if dropped_items:
        # S-1 — say what happened rather than leaving the shopper to notice
        # the total is smaller than they remember.
        messages.info(
            request,
            f'{", ".join(dropped_items)} — no longer available at this '
            f'branch, so removed from your cart.'
        )
    remaining = max(FREE_DELIVERY_OVER - total, Decimal('0'))
    # One-time checkout token — see checkout()'s own comment for what this
    # actually guards against. setdefault so a plain reload of the cart page
    # (no checkout attempt in between) doesn't rotate it out from under a
    # form the shopper already has open in another tab.
    request.session.setdefault('checkout_token', uuid.uuid4().hex)
    branch = Branch.objects.filter(
        id=request.session.get('branch_id'), is_active=True
    ).first()
    location_used = customer_fulfillment_location(request.user, request.session)
    return render(request, 'orders/cart.html', {
        'items': items,
        'total': total,
        'checkout_token': request.session['checkout_token'],
        'free_delivery_over': FREE_DELIVERY_OVER,
        'remaining_for_free': remaining,
        'qualifies': total >= FREE_DELIVERY_OVER,
        'progress_pct': min(int((total / FREE_DELIVERY_OVER) * 100), 100) if total else 0,
        'branch': branch,
        'assignment_distance': _assignment_distance(request, branch),
        'location_used': location_used,
        # The one saved address a Delivery order ships to. Shown read-only in
        # the checkout form; the shopper changes it in Settings -> Addresses,
        # never here. checkout() re-reads the same row.
        'default_address': request.user.addresses.filter(is_default=True).first(),
        'payment_choices': [
            ('Cash on Delivery', 'Cash on Delivery'),
            ('Pay at Store', 'Pay at Store'),
            ('GCash', 'GCash'),
        ],
        'cart_count': len(request.session.get('cart', {})),
    })


@customer_required
def checkout(request):
    branch_id = request.session.get('branch_id')
    cart = request.session.get('cart', {})

    if not cart:
        messages.error(request, 'Your cart is empty.')
        return redirect('orders:catalog')

    # Items but no branch: a distinct state from an empty cart, and it gets a
    # distinct answer — pick a branch, don't be told the cart is empty when it
    # is not. In practice only reachable from a session predating the rule
    # that a cart always has a branch.
    if not branch_id:
        messages.error(request, PICK_BRANCH_TO_CHECK_OUT)
        return redirect('orders:select_branch')

    if request.method != 'POST':
        # There is no checkout PAGE — the form lives on the cart — so a GET
        # here is a bookmark, a refresh or the back button, never the form.
        # Nothing is written either way, but bouncing to the cart without a
        # word looks like a checkout page that failed to load.
        messages.info(request, 'Review your order below, then press Place Order.')
        return redirect('orders:cart')

    # Double-submit guard. The button disables itself on click (cart.html),
    # but that alone does nothing against a slow connection where the first
    # response hasn't come back yet when the shopper taps again, or a
    # replayed/retried POST — either way, two requests can reach this view
    # for the one "Place Order" click.
    #
    # cache.add() is the actual guard: it is one atomic "set only if absent"
    # operation, so of two requests racing on the same token, only one can
    # ever win it, however close together they arrive — a session read/write
    # alone cannot promise that (both could read the token as still valid
    # before either request saves back an updated session). The session
    # token itself is a second, narrower check: whether this request even
    # belongs to the cart page currently in the session, so a bookmarked or
    # cached stale form can't submit an order for a cart that no longer
    # matches what is displayed.
    submitted_token = request.POST.get('checkout_token', '')
    session_token = request.session.get('checkout_token', '')
    try:
        # A cache backend error here must fail OPEN (let checkout proceed),
        # not crash the page — this token only stops a duplicate order when
        # stock is plentiful enough for both to independently succeed; the
        # real anti-oversell guard is the select_for_update() lock in the
        # atomic block below, which doesn't depend on the cache at all. A
        # rare double-submit slipping through during a Redis outage is a
        # far smaller problem than nobody being able to check out at all.
        token_already_used = not cache.add(f'checkout_token_used:{submitted_token}', True, 300)
    except Exception:
        token_already_used = False
    if not session_token or submitted_token != session_token or token_already_used:
        messages.info(request, "This order may already be on its way — check My Orders.")
        return redirect('orders:my_orders')

    # Rotate now, not after the order is created — this is what stops a
    # THIRD, slower-arriving copy of the same double-tap (which read the old
    # token before this request saved a new one) from creating a second
    # order once this one succeeds.
    request.session['checkout_token'] = uuid.uuid4().hex

    displayed_branch_id = branch_id
    resolution = _resolve_cart_branch(request, cart, notify_change=True)
    if not resolution.branch:
        messages.error(request, NO_SINGLE_BRANCH)
        return redirect('orders:cart')
    branch_id = resolution.branch.pk
    if branch_id != displayed_branch_id:
        messages.info(
            request,
            'Your fulfillment branch changed after your location or address '
            'was updated. Review the revalidated cart before placing the order.',
        )
        return redirect('orders:cart')

    # S-1 — resolve the cart through the same helper the cart page itself
    # renders from, and let it prune any line that can no longer be priced at
    # all (delisted, out at this branch, its last lot expired) BEFORE the rest
    # of checkout ever looks at the raw session cart below. Without this, a
    # line the cart page had already stopped showing (view_cart calls the
    # same helper) was still sitting in request.session['cart'], and checkout
    # met it here for the first time — rejecting a product the shopper could
    # no longer even see, for a total that didn't match what was on screen.
    _, _, dropped_items = _cart_items(request)
    if dropped_items:
        messages.error(
            request,
            f'{", ".join(dropped_items)} — no longer available at this '
            f'branch, removed from your cart. Please review your order.'
        )
        return redirect('orders:cart')
    cart = request.session.get('cart', {})
    if not cart:
        messages.error(request, 'Your cart is empty.')
        return redirect('orders:catalog')


    # --- payment method --------------------------------------------------------
    # Validated against the model's choices, the same fall-back-to-default
    # shape discount_type uses at the counter: an unrecognised value — a
    # tampered form, a client bug — becomes the online default (Cash on
    # Delivery) rather than being written to the column and the billing
    # record verbatim, which broke the Billing report's payment breakdown.
    #
    # Falling back is right here and wrong for fulfillment_type just below;
    # the difference is that every payment method on this form is recorded
    # only, and settled in person either way, so the default cannot mislead
    # anyone about what happens next.
    mode_of_payment = (
        request.POST.get('mode_of_payment') or SalesTransaction.ModeOfPayment.COD
    )
    if mode_of_payment not in SalesTransaction.ModeOfPayment.values:
        mode_of_payment = SalesTransaction.ModeOfPayment.COD

    # --- GCash reference ----------------------------------------------------
    # No payment gateway is integrated (Chapter 1, Limitation 1), so for a
    # GCash order the reference the shopper types IS the record: it is the
    # only handle anyone has when matching the transfer against the billing
    # row. Accepting a blank one wrote a row reading "GCash" with nothing to
    # reconcile it against.
    #
    # This refuses rather than falling back, for the same reason
    # fulfillment_type below does: there is no safe value to invent. The
    # opposite error was live too — the field was written for EVERY method,
    # so a reference left in the box before switching to Cash on Delivery
    # rode along onto a row that has no reference by definition. Hence the
    # clear on the else branch.
    #
    # The same goes for the screenshot: a GCash payment now needs both on
    # file before staff can verify it (BillingRecord.missing_payment_evidence),
    # and only the shopper has the screenshot — so it is asked for here
    # rather than leaving every online GCash order impossible to verify.
    reference_number = request.POST.get('reference_number', '').strip()
    proof_of_payment = None
    if mode_of_payment == SalesTransaction.ModeOfPayment.GCASH:
        from billing.models import BillingRecord
        from billing.validators import clean_reference_number
        from billing.views import _validate_proof_of_payment

        proof_of_payment = request.FILES.get('proof_of_payment')
        # Length and characters first — the column is VARCHAR(100), and an
        # over-long reference used to reach it and 500 mid-checkout. The
        # cleaned value is what is stored. billing.validators is the rule
        # billing_create and the pay API apply too.
        try:
            reference_number = clean_reference_number(
                reference_number, mode_of_payment
            )
        except ValidationError as e:
            messages.error(request, e.messages[0])
            return redirect('orders:cart')
        missing = BillingRecord.missing_payment_evidence(
            mode_of_payment, reference_number, bool(proof_of_payment)
        )
        if missing:
            messages.error(
                request,
                f'Enter your GCash {" and ".join(missing)}, or choose another '
                f'payment method — we cannot match the payment to your order '
                f'without it.'
            )
            return redirect('orders:cart')
        try:
            _validate_proof_of_payment(proof_of_payment)
        except ValidationError as e:
            messages.error(request, e.message)
            return redirect('orders:cart')
    else:
        reference_number = ''

    # --- delivery vs pickup -------------------------------------------------
    # F-11 — delivery_address used to be written straight from the POST with
    # no validation at all, and the form invited a blank one ("leave blank to
    # pick up"). A delivery order whose address field was lost — a client bug,
    # a mis-tap, a stripped form — was therefore indistinguishable from a
    # genuine pickup: same blank column, no way for the Order Console to tell
    # that anyone was ever meant to deliver it.
    #
    # So the choice is now explicit and required, and the address is validated
    # against it. Unlike mode_of_payment above, an unrecognised
    # value must NOT fall back to a default: silently picking one would either
    # promise a delivery nobody asked for or strand a customer waiting at home
    # for an order marked pickup. There is no safe guess, so this refuses.
    fulfillment_type = request.POST.get('fulfillment_type') or ''
    if fulfillment_type not in SalesTransaction.FulfillmentType.values:
        messages.error(
            request, 'Choose whether this order is for delivery or for pickup.'
        )
        return redirect('orders:cart')

    # Pay at Store means exactly that — settled in person at the counter,
    # which a Delivery order never puts anyone at. cart.html already flags
    # this combination and blocks the submit client-side; this is the same
    # rule enforced server-side, for a tampered form or a JS-less client.
    if (
        fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY
        and mode_of_payment == SalesTransaction.ModeOfPayment.PAY_AT_STORE
    ):
        messages.error(
            request,
            'Pay at Store isn’t available for delivery orders — choose '
            'Cash on Delivery or GCash, or switch to pickup at the branch.'
        )
        return redirect('orders:cart')

    # The address is NOT read from this form — there is no address input on it
    # any more. A Delivery order ships to the address saved on the account
    # (the default Address row), captured at registration or in
    # Settings -> Addresses and edited only there. One authoritative,
    # structured, map-pinned record beats a free-text box retyped — and
    # mistyped — on every order.
    #
    # No saved address while Delivery is chosen: refused, not defaulted, for
    # the same reason an unrecognised fulfillment_type is refused above —
    # there is nothing safe to invent. Pickup never carries an address, so a
    # missing one there is no problem.
    if fulfillment_type == SalesTransaction.FulfillmentType.DELIVERY:
        saved = request.user.addresses.filter(is_default=True).first()
        delivery_address = saved.text.strip() if saved else ''
        if not delivery_address:
            messages.error(
                request,
                'Add a delivery address to your account before placing a '
                'delivery order — open Settings then Addresses. Or choose '
                'Pickup to collect this order at the branch.'
            )
            return redirect('orders:cart')
    else:  # Pickup — never carries an address, whatever is on the account.
        delivery_address = ''

    # --- PWD / Senior Citizen discount --------------------------------------
    # Deliberately NOT read from this form, and there is no UI offering it.
    #
    # F-04 — checkout used to take discount_type straight from the POST and
    # apply the full statutory 20% + VAT exemption on it. The only guard was
    # that the ID-number and holder-name fields be non-empty strings, so
    # 'MADE-UP-123' bought a permanent 20% discount; no staff member ever saw
    # a card. The discount is conditioned on presenting a PWD or OSCA ID, and
    # nothing typed into a web form is presenting anything.
    #
    # So every order this view writes is undiscounted, whatever the request
    # contains. Where a discount is genuinely owed, staff apply it afterwards
    # from the Order Console (sales.views.transaction_apply_discount), which
    # records who verified the ID — and which refuses outright on a Delivery
    # order, because nobody at the branch ever meets that customer.
    #
    # This matches the API write path next door, which has always passed
    # discount_type=None for the same reason (SalesTransactionWriteSerializer).
    #
    # P15 — the customer MAY now ask for the PWD discount on an order they
    # collect in store (Pickup, which is also the only fulfilment Pay at Store
    # is allowed with). That is a request, not a claim: it records the ID
    # details they typed and changes no price. The order is still written
    # undiscounted below, and staff approve or reject the request when the
    # customer arrives (sales.pwd_requests, transaction_pwd_request_review);
    # billing will not mark it Paid until they have. The discount_type field
    # and the discount_id_number / discount_holder_name inputs stay ignored,
    # exactly as above — the request has its own, differently named fields.
    pwd_request = None
    if request.POST.get('request_pwd_discount'):
        if fulfillment_type != SalesTransaction.FulfillmentType.PICKUP:
            messages.error(
                request,
                'A PWD discount can only be requested for orders you pick up '
                'at the branch — staff have to see your ID in person. Choose '
                'pickup, or place the order without the request.'
            )
            return redirect('orders:cart')
        pwd_id, pwd_name, pwd_errors = validate_pwd_details(
            request.POST.get('pwd_id_number'), request.POST.get('pwd_holder_name')
        )
        if pwd_errors:
            for error in pwd_errors:
                messages.error(request, error)
            return redirect('orders:cart')
        pwd_request = (pwd_id, pwd_name)

    with transaction.atomic():
        # Lock every relevant active-branch stock row in a stable order before
        # the final resolution. A concurrent checkout for any of these units
        # must wait, then sees this order's newly committed SalesItems before
        # it can promise the same stock.
        list(
            Inventory.objects.select_for_update()
            .filter(
                branch__is_active=True,
                product_id__in=[int(product_id) for product_id in cart],
            )
            .order_by('branch_id', 'product_id')
        )
        locked_resolution = _resolve_cart_branch(request, cart)
        if not locked_resolution.branch:
            messages.error(request, NO_SINGLE_BRANCH)
            transaction.set_rollback(True)
            return redirect('orders:cart')
        if locked_resolution.branch.pk != branch_id:
            remember_assignment(request.session, locked_resolution)
            messages.info(
                request,
                f'Availability changed while you were checking out. Your cart '
                f'is now fulfilled by {locked_resolution.branch.branch_name}; '
                'review it before placing the order.',
            )
            transaction.set_rollback(True)
            return redirect('orders:cart')

        # Validate availability WITHOUT deducting: an online order only leaves
        # the shelf when staff mark it Completed in the Order Console.
        planned = []
        # Lines that don't resolve to a real Inventory row at all — the
        # product, or its row at this branch, was deleted after it went into
        # the cart. Distinct from a row that exists and genuinely reads 0
        # available: that case still refuses checkout below with a normal
        # "not enough left" message the shopper can act on. This one can
        # never be fixed by the shopper, because _cart_items already skips it
        # from the page they'd use to remove it — so it's purged from the
        # session cart here instead of being allowed to block checkout
        # forever on an item nobody can see any more.
        stale_ids = []
        available_by_product = {
            product_id: quantity
            for (available_branch_id, product_id), quantity in fulfillable_quantities(
                [branch_id], cart.keys()
            ).items()
            if available_branch_id == branch_id
        }
        for product_id, qty in cart.items():
            inv = Inventory.objects.select_for_update().sellable().select_related(
                'product'
            ).filter(product_id=product_id, branch_id=branch_id).first()
            if inv is None:
                stale_ids.append(product_id)
                continue
            # available_qty already nets out anything expired since this
            # was added to the cart — it may be less than the row's raw
            # quantity_on_hand, which still counts expired stock.
            available_qty = available_by_product.get(inv.product_id, 0)
            if available_qty < qty:
                messages.error(
                    request,
                    f'Sorry — only {available_qty} of {inv.product.product_name} left.'
                )
                transaction.set_rollback(True)
                return redirect('orders:cart')
            planned.append((inv.product, qty))

        if stale_ids:
            for pid in stale_ids:
                cart.pop(pid, None)
            request.session['cart'] = cart
            messages.error(
                request,
                'Some items in your cart are no longer available and were '
                'removed. Please review your cart and try again.'
            )
            transaction.set_rollback(True)
            return redirect('orders:cart')

        if not planned:
            messages.error(request, 'Your cart is empty.')
            transaction.set_rollback(True)
            return redirect('orders:catalog')

        # Same pricing engine as the counter. This used to be a raw
        # unit_price x qty sum, which meant an online order recorded no VAT
        # breakdown at all (total_vat_amount stayed 0 on every one of them)
        # and could not honour a wholesale tier. price_cart owns the VAT
        # arithmetic; the only decision made out here is the price TIER.
        #
        # A request that could not move the total is refused rather than
        # left Pending for staff to chase — PWD eligibility is per product.
        if pwd_request and not request_can_reach_cart(p for p, _ in planned):
            messages.error(
                request,
                'None of the items in your cart are eligible for the PWD '
                'discount, so there is nothing to request. Place the order '
                'without the request.'
            )
            transaction.set_rollback(True)
            return redirect('orders:cart')

        # No discount_type is passed — not None-by-omission but None on
        # purpose: customer checkout never discounts (F-04). A PWD request
        # (P15) does not change that: it is priced at full below.
        priced = price_cart(
            [(product, qty, product.price_at(qty)) for product, qty in planned],
            discount_type=None,
        )
        total = priced['total_amount']

        sale = SalesTransaction.objects.create(
            branch_id=branch_id,
            staff=None,
            customer=request.user,
            transaction_type=SalesTransaction.TransactionType.ONLINE,
            order_status=SalesTransaction.OrderStatus.PENDING,
            mode_of_payment=mode_of_payment,
            fulfillment_type=fulfillment_type,
            delivery_address=delivery_address,
            # Priced up front and written on the INSERT itself — the same
            # order sales.views.transaction_add and the DRF write serializer
            # already use. This used to create at the model's total_amount=0
            # default and patch it a beat later, which meant billing.signals'
            # auto-created record briefly existed at amount_due=0 (and, on a
            # crash between the two saves, could be left there).
            total_amount=total,
            total_vat_amount=priced['total_vat_amount'],
            total_discount_amount=priced['total_discount_amount'],
            **(new_request_fields(*pwd_request) if pwd_request else {}),
        )

        for line in priced['lines']:
            SalesItem.objects.create(
                transaction=sale, product=line['product'],
                quantity_sold=line['quantity'],
                # Already-discounted unit price — SalesItem.save multiplies it
                # out, so the discount lives in the price and must not be
                # taken off the subtotal a second time.
                unit_price=line['unit_price'],
                is_vat_exempt_line=line['is_vat_exempt_line'],
                vat_amount=line['vat_amount'],
                discount_amount=line['discount_amount'],
            )

        # billing.signals already created the record, at the correct
        # amount_due now that total_amount is set at creation — only the
        # payment details it couldn't have known belong here.
        billing = getattr(sale, 'billing_record', None)
        if billing:
            billing.payment_method = sale.mode_of_payment
            # Validated above: non-blank exactly when the method is GCash,
            # blank for every other method. Not re-read from the POST here —
            # that is what let an unvalidated value onto the row.
            billing.reference_number = reference_number
            if proof_of_payment:
                billing.proof_of_payment = proof_of_payment
            billing.save()

    if pwd_request:
        notify_pwd_request_submitted(sale)

    request.session['cart'] = {}
    request.session.pop('fulfillment_location', None)
    request.session.pop('fulfillment_assignment', None)
    messages.success(request, f'Order {sale.order_number} placed!')
    if pwd_request:
        messages.info(
            request,
            'Your PWD discount request is pending. Bring your PWD ID when you '
            'collect the order — the store checks it there, and the discount '
            'is applied to your bill once it is verified.'
        )
    return redirect('orders:confirmation', transaction_id=sale.id)


# ─────────────────────────────────────────────
# MY ORDERS / TRACK ORDER
# The Context Diagram says the Customer receives order status back from the
# system — this is that. Strictly scoped to the logged-in customer's own orders.
# ─────────────────────────────────────────────

# What the customer is told at each stage, in plain language.
STATUS_STEPS = ['Pending', 'Approved', 'Preparing', 'Completed']
STATUS_BLURB = {
    'Pending': "We've received your order. The store will confirm it shortly.",
    'Approved': 'The store has confirmed your order and will prepare it soon.',
    'Preparing': 'Your order is being packed right now.',
    'Completed': 'Done — your order has been handed over.',
    'Cancelled': 'This order was cancelled. Contact the branch if that looks wrong.',
}


@customer_required
def my_orders(request):
    orders = (
        SalesTransaction.objects.filter(customer=request.user)
        .select_related('branch')
        .prefetch_related('billing_record')
        .order_by('-transaction_date')
    )

    # Which lifecycle statuses each tab stands for. One table, so the tab a
    # shopper picks and the rows they get back cannot drift apart.
    #
    # It used to be read twice, the second time to build a `counts` dict of
    # five .count() queries for numbers beside each tab label. The tabs render
    # as plain text by design, so nothing ever read them: five queries per
    # page load whose results went nowhere.
    #
    # 'active' used to mean everything that was neither Completed nor
    # Cancelled, which included Preparing. Now that Preparing has a tab of its
    # own ("To receive" — the order is packed and waiting to be handed over),
    # leaving it in both would have made the two counts overlap and the totals
    # across the bar exceed All. In progress is therefore the part of the
    # lifecycle the STORE is still working through, and To receive the part
    # the CUSTOMER is waiting on.
    tab_statuses = {
        'active': ['Pending', 'Approved'],
        'to_receive': ['Preparing'],
        'completed': ['Completed'],
        'cancelled': ['Cancelled'],
    }

    status_filter = request.GET.get('status', '')
    if status_filter not in tab_statuses:
        status_filter = ''          # an unknown ?status= falls back to All

    if status_filter:
        orders = orders.filter(order_status__in=tab_statuses[status_filter])

    return render(request, 'orders/my_orders.html', {
        'orders': orders,
        'status_filter': status_filter,
        'active_page': 'orders',
    })


@customer_required
def track_order(request, transaction_id):
    """Follow one order through its stages."""
    sale = get_object_or_404(
        SalesTransaction.objects.select_related('branch'),
        id=transaction_id, customer=request.user,   # never someone else's
    )

    cancelled = sale.order_status == 'Cancelled'
    completed = sale.order_status == 'Completed'
    current = 0 if cancelled else STATUS_STEPS.index(sale.order_status)
    steps = [
        {
            'name': name,
            # A Completed order has finished its last step, not started it —
            # the loop's own `current` index still points at it, so a
            # Completed order needs that step marked done too, not just
            # everything before it.
            'done': (not cancelled) and (i < current or (completed and i == current)),
            'current': (not cancelled) and (i == current) and not completed,
        }
        for i, name in enumerate(STATUS_STEPS)
    ]

    return render(request, 'orders/track_order.html', {
        'sale': sale,
        'items': sale.items.select_related('product').all(),
        'billing': getattr(sale, 'billing_record', None),
        'steps': steps,
        'cancelled': cancelled,
        'blurb': STATUS_BLURB.get(sale.order_status, ''),
    })


@customer_required
def order_confirmation(request, transaction_id):
    sale = get_object_or_404(
        SalesTransaction.objects.select_related('branch'),
        id=transaction_id, customer=request.user,
    )
    return render(request, 'orders/confirmation.html', {
        'sale': sale,
        'items': sale.items.select_related('product').prefetch_related('product__images'),
        'billing': getattr(sale, 'billing_record', None),
    })
